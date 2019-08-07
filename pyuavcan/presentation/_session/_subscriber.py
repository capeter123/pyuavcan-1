#
# Copyright (c) 2019 UAVCAN Development Team
# This software is distributed under the terms of the MIT License.
# Author: Pavel Kirienko <pavel.kirienko@zubax.com>
#

from __future__ import annotations
import typing
import logging
import asyncio
import dataclasses
import pyuavcan.util
import pyuavcan.dsdl
import pyuavcan.transport
from ._base import MessagePresentationSession, MessageClass, TypedSessionFinalizer, Closable
from ._error import PresentationSessionClosedError


# Shouldn't be too large as this value defines how quickly the task will detect that the underlying transport is closed.
_RECEIVE_TIMEOUT = 1


_logger = logging.getLogger(__name__)


#: Type of the async received message handler callable.
ReceivedMessageHandler = typing.Callable[[MessageClass, pyuavcan.transport.TransferFrom], typing.Awaitable[None]]


@dataclasses.dataclass
class SubscriberStatistics:
    transport_session:        pyuavcan.transport.Statistics  #: Shared for all subscribers with same session specifier.
    messages:                 int  #: Number of received messages, individual per subscriber.
    overruns:                 int  #: Number of messages lost to queue overruns; individual per subscriber.
    deserialization_failures: int  #: Number of messages lost to deserialization errors; shared per session specifier.


class Subscriber(MessagePresentationSession[MessageClass]):
    """
    A task should request its own independent subscriber instance from the presentation layer controller.
    Do not share the same subscriber instance across different tasks. This class implements the RAII pattern.

    Whenever a message is received from a subject, it is deserialized once and the resulting object is
    passed by reference into each subscriber instance. If there is more than one subscriber instance for
    a subject, accidental mutation of the object by one consumer may affect other consumers. To avoid this,
    the application should either avoid mutating received message objects or clone them beforehand.

    This class implements the async iterator protocol yielding received messages.
    Iteration stops shortly after the subscriber is closed.
    It can be used as follows::

        async for message, transfer in subscriber:
            ...  # Handle the message.
        # The loop will be stopped shortly after the subscriber is closed.

    Implementation info: all subscribers sharing the same session specifier also share the same
    underlying implementation object containing the transport session which is reference counted and destroyed
    automatically when the last subscriber with that session specifier is closed;
    the user code cannot access it and generally shouldn't care.
    """
    def __init__(self,
                 impl:           SubscriberImpl[MessageClass],
                 loop:           asyncio.AbstractEventLoop,
                 queue_capacity: typing.Optional[int]):
        """
        Do not call this directly! Use :meth:`Presentation.make_subscriber`.
        """
        if queue_capacity is None:
            queue_capacity = 0      # This case is defined by the Queue API. Means unlimited.
        else:
            queue_capacity = int(queue_capacity)
            if queue_capacity < 1:
                raise ValueError(f'Invalid queue capacity: {queue_capacity}')

        self._closed = False
        self._impl = impl
        self._loop = loop
        self._maybe_task: typing.Optional[asyncio.Task[None]] = None
        self._rx: _Listener[MessageClass] = _Listener(asyncio.Queue(maxsize=queue_capacity, loop=loop))
        impl.add_listener(self._rx)

    # ----------------------------------------  HANDLER-BASED API  ----------------------------------------

    def receive_in_background(self, handler: ReceivedMessageHandler[MessageClass]) -> None:
        """
        Configures the subscriber to invoke the specified handler whenever a message is received.

        If the caller attempts to configure multiple handlers by invoking this method repeatedly,
        only the last configured handler will be active (the old ones will be forgotten).
        If the handler throws an exception, it will be suppressed and logged.

        This method internally starts a new task. If the subscriber is closed while the task is running,
        the task will be silently cancelled automatically; the application need not get involved.

        This method of handling messages should not be used with the plain async receive API;
        an attempt to do so may lead to unpredictable message distribution between consumers.
        """
        async def task_function() -> None:
            # This could be an interesting opportunity for optimization: instead of using the queue, just let the
            # implementation class invoke the handler from its own receive task directly. Eliminates extra indirection.
            while not self._closed:
                try:
                    message, transfer = await self.receive()
                    try:
                        await handler(message, transfer)
                    except asyncio.CancelledError:
                        raise
                    except Exception as ex:
                        _logger.exception('%s got an unhandled exception in the message handler: %s', self, ex)
                except asyncio.CancelledError:
                    _logger.debug('%s receive task cancelled', self)
                    break
                except pyuavcan.transport.ResourceClosedError as ex:
                    _logger.info('%s receive task got a resource closed error and will exit: %s', self, ex)
                    break
                except Exception as ex:
                    _logger.exception('%s receive task failure: %s', self, ex)
                    await asyncio.sleep(1)  # TODO is this an adequate failure management strategy?

        if self._maybe_task is not None:
            self._maybe_task.cancel()

        self._maybe_task = self._loop.create_task(task_function())

    # ----------------------------------------  DIRECT RECEIVE  ----------------------------------------

    async def receive(self) -> typing.Tuple[MessageClass, pyuavcan.transport.TransferFrom]:
        """
        This is like :meth:`receive_for` with an infinite timeout.
        """
        while True:
            out = await self.receive_for(_RECEIVE_TIMEOUT)
            if out is not None:
                return out

    async def receive_until(self, monotonic_deadline: float) \
            -> typing.Optional[typing.Tuple[MessageClass, pyuavcan.transport.TransferFrom]]:
        """
        This is like :meth:`receive_for` with deadline instead of timeout.
        The deadline value is compared against :meth:`asyncio.AbstractEventLoop.time`.
        A deadline that is in the past translates into negative timeout.
        """
        return await self.receive_for(timeout=monotonic_deadline - self._loop.time())

    async def receive_for(self, timeout: float) \
            -> typing.Optional[typing.Tuple[MessageClass, pyuavcan.transport.TransferFrom]]:
        """
        Blocks until either a valid message is received,
        in which case it is returned along with the transfer which delivered it;
        or until the timeout is expired, in which case None is returned.

        The method will never return None unless the timeout has expired or its session is closed;
        in order words, a spurious premature cancellation cannot occur.

        If the timeout is non-positive, the method will non-blockingly check if there is any data;
        if there is, it will be returned, otherwise None will be returned immediately.
        It is guaranteed that no context switch will occur if the timeout is negative, as if the method was not async.
        """
        self._raise_if_closed_or_failed()
        try:
            if timeout > 0:
                message, transfer = await asyncio.wait_for(self._rx.queue.get(), timeout, loop=self._loop)
            else:
                message, transfer = self._rx.queue.get_nowait()
        except asyncio.QueueEmpty:
            return None
        except asyncio.TimeoutError:
            return None
        else:
            assert isinstance(message, self._impl.dtype), 'Internal protocol violation'
            assert isinstance(transfer, pyuavcan.transport.TransferFrom), 'Internal protocol violation'
            return message, transfer

    # ----------------------------------------  ITERATOR API  ----------------------------------------

    def __aiter__(self) -> Subscriber[MessageClass]:
        """
        Iterator API support. Returns self unchanged.
        """
        return self

    async def __anext__(self) -> typing.Tuple[MessageClass, pyuavcan.transport.TransferFrom]:
        """
        This is just a wrapper over :meth:`receive`.
        """
        try:
            return await self.receive()
        except pyuavcan.transport.ResourceClosedError:
            raise StopAsyncIteration

    # ----------------------------------------  AUXILIARY  ----------------------------------------

    @property
    def dtype(self) -> typing.Type[MessageClass]:
        return self._impl.dtype

    @property
    def transport_session(self) -> pyuavcan.transport.InputSession:
        return self._impl.transport_session

    def sample_statistics(self) -> SubscriberStatistics:
        """
        Returns the statistical counters of this subscriber, including the statistical metrics of the underlying
        transport session, which is shared across all subscribers with the same session specifier.
        """
        return SubscriberStatistics(transport_session=self.transport_session.sample_statistics(),
                                    messages=self._rx.push_count,
                                    deserialization_failures=self._impl.deserialization_failure_count,
                                    overruns=self._rx.overrun_count)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._impl.remove_listener(self._rx)
            if self._maybe_task is not None:    # The task may be holding the lock.
                try:
                    self._maybe_task.cancel()   # We don't wait for it to exit because it's pointless.
                except Exception as ex:
                    _logger.exception('%s task could not be cancelled: %s', self, ex)
                self._maybe_task = None

    def _raise_if_closed_or_failed(self) -> None:
        if self._closed:
            raise PresentationSessionClosedError(repr(self))

        if self._rx.exception is not None:
            self._closed = True
            raise self._rx.exception from RuntimeError('The subscriber has failed and been closed')

    def __del__(self) -> None:
        if not self._closed:
            _logger.info('%s has not been disposed of properly; fixing', self)
            self._closed = True
            self._impl.remove_listener(self._rx)


@dataclasses.dataclass
class _Listener(typing.Generic[MessageClass]):
    """
    The queue-induced extra level of indirection adds processing overhead and latency. In the future we may need to
    consider an optimization where the subscriber would automatically detect whether the underlying implementation
    is shared among many subscribers or not. If not, it should bypass the queue and read from the transport directly
    instead. This would avoid the unnecessary overheads and at the same time would be transparent for the user.
    """
    queue:         asyncio.Queue[typing.Tuple[MessageClass, pyuavcan.transport.TransferFrom]]
    push_count:    int = 0
    overrun_count: int = 0
    exception:     typing.Optional[Exception] = None

    def push(self, message: MessageClass, transfer: pyuavcan.transport.TransferFrom) -> None:
        try:
            self.queue.put_nowait((message, transfer))
            self.push_count += 1
        except asyncio.QueueFull:
            self.overrun_count += 1


class SubscriberImpl(Closable, typing.Generic[MessageClass]):
    """
    This class implements the actual reception and deserialization logic. It is not visible to the user and is not
    part of the API. There is at most one instance per session specifier. It may be shared across multiple users
    with the help of the proxy class. When the last proxy is closed or garbage collected, the implementation will
    also be closed and removed.
    """
    def __init__(self,
                 dtype:             typing.Type[MessageClass],
                 transport_session: pyuavcan.transport.InputSession,
                 finalizer:         TypedSessionFinalizer,
                 loop:              asyncio.AbstractEventLoop):
        self.dtype = dtype
        self.transport_session = transport_session
        self.deserialization_failure_count = 0
        self._finalizer = finalizer
        self._loop = loop
        self._task = loop.create_task(self._task_function())
        self._listeners: typing.List[_Listener[MessageClass]] = []
        self._closed = False

    async def _task_function(self) -> None:
        exception: typing.Optional[Exception] = None
        try:
            while not self._closed:
                transfer = await self.transport_session.receive_until(self._loop.time() + _RECEIVE_TIMEOUT)
                if transfer is not None:
                    message = pyuavcan.dsdl.deserialize(self.dtype, transfer.fragmented_payload)
                    if message is not None:
                        for rx in self._listeners:
                            rx.push(message, transfer)
                    else:
                        self.deserialization_failure_count += 1
        except asyncio.CancelledError:
            _logger.info('Cancelling the subscriber task of %s', self)
        except Exception as ex:
            exception = ex
            # Do not use f-string because it can throw, unlike the built-in formatting facility of the logger
            _logger.exception('Fatal error in the subscriber task of %s: %s', self, ex)

        try:
            self._closed = True
            self._finalizer([self.transport_session])
        except Exception as ex:
            exception = ex
            # Do not use f-string because it can throw, unlike the built-in formatting facility of the logger
            _logger.exception('Failed to finalize %s: %s', self, ex)

        exception = exception if exception is not None else PresentationSessionClosedError(repr(self))
        for rx in self._listeners:
            rx.exception = exception

    def close(self) -> None:
        self._closed = True
        try:
            self._task.cancel()         # Force the task to be stopped ASAP without waiting for timeout
        except Exception as ex:
            _logger.debug('Explicit close: could not cancel the task %r: %s', self._task, ex, exc_info=True)

    def add_listener(self, rx: _Listener[MessageClass]) -> None:
        self._raise_if_closed()
        self._listeners.append(rx)

    def remove_listener(self, rx: _Listener[MessageClass]) -> None:
        # Removal is always possible, even if closed.
        try:
            self._listeners.remove(rx)
        except ValueError:
            _logger.exception('%r does not have listener %r', self, rx)
        if len(self._listeners) == 0 and not self._closed:
            self._closed = True
            try:
                self._task.cancel()         # Force the task to be stopped ASAP without waiting for timeout
            except Exception as ex:
                _logger.debug('Listener removal: could not cancel the task %r: %s', self._task, ex, exc_info=True)

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise PresentationSessionClosedError(repr(self))

    def __repr__(self) -> str:
        return pyuavcan.util.repr_attributes_noexcept(self,
                                                      dtype=str(pyuavcan.dsdl.get_model(self.dtype)),
                                                      transport_session=self.transport_session,
                                                      deserialization_failure_count=self.deserialization_failure_count,
                                                      listeners=self._listeners,
                                                      closed=self._closed)
