"""Validate upload parts and enforce the aggregate limit before spooling."""

from typing import TYPE_CHECKING, get_args, get_origin

from fastapi import HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from python_multipart.multipart import parse_options_header
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect

from definitions.config import get_capture_upload_settings
from definitions.staging import CaptureUploadTooLargeError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Coroutine
    from typing import Any

    from starlette.datastructures import FormData, Headers
    from starlette.responses import Response
    from starlette.types import Receive, Scope


class _InvalidMultipartField(Exception):
    """Carry only trusted validation metadata, never a submitted field name."""

    def __init__(self, *, code: str, field: str | None = None) -> None:
        self.code = code
        self.field = field


def _is_repeatable(annotation: object) -> bool:
    """Recognize list-valued Form/File parameters, including optional lists."""
    return get_origin(annotation) is list or any(
        get_origin(option) is list for option in get_args(annotation)
    )


class _CaptureMultiPartParser(MultiPartParser):
    def __init__(
        self,
        headers: Headers,
        stream: AsyncGenerator[bytes],
        *,
        max_upload_bytes: int,
        max_files: int | float,
        max_fields: int | float,
        max_part_size: int,
        allowed_fields: frozenset[str],
        repeatable_fields: frozenset[str],
    ) -> None:
        super().__init__(
            headers,
            stream,
            max_files=max_files,
            max_fields=max_fields,
            max_part_size=max_part_size,
        )
        self._max_upload_bytes = max_upload_bytes
        self._total_file_bytes = 0
        self._allowed_fields = allowed_fields
        self._repeatable_fields = repeatable_fields
        self._seen_fields: set[str] = set()

    def on_headers_finished(self) -> None:
        """Reject an unknown or repeated singleton before its payload is stored."""
        _disposition, options = parse_options_header(self._current_part.content_disposition)
        raw_name = options.get(b"name")
        if raw_name is not None:
            try:
                name = raw_name.decode(self._charset)
            except UnicodeDecodeError, LookupError:
                name = raw_name.decode("latin-1")
            if name not in self._allowed_fields:
                raise _InvalidMultipartField(code="extra_forbidden")
            if name in self._seen_fields and name not in self._repeatable_fields:
                raise _InvalidMultipartField(code="duplicate_field", field=name)
            self._seen_fields.add(name)
        super().on_headers_finished()

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        # Count file bytes before spooling. Framing and ordinary form fields
        # do not consume the capture budget.
        if self._current_part.file is not None:
            self._total_file_bytes += end - start
            if self._total_file_bytes > self._max_upload_bytes:
                raise CaptureUploadTooLargeError
        super().on_part_data(data, start, end)

    def close_files(self) -> None:
        """Close even unfinished spools after disconnects or task cancellation."""
        # Starlette tracks all spools here, but only cleans parser/OSError paths.
        # Use synchronous close as it does so cancellation cannot interrupt it.
        for spool in self._files_to_close_on_error:
            spool.close()


class _CaptureRequest(Request):
    multipart_parser: _CaptureMultiPartParser | None = None

    def __init__(
        self,
        scope: Scope,
        receive: Receive,
        *,
        allowed_fields: frozenset[str],
        repeatable_fields: frozenset[str],
    ) -> None:
        super().__init__(scope, receive)
        self._allowed_fields = allowed_fields
        self._repeatable_fields = repeatable_fields

    async def _get_form(
        self,
        *,
        max_files: int | float = 1000,
        max_fields: int | float = 1000,
        max_part_size: int = 1024 * 1024,
    ) -> FormData:
        content_type, _ = parse_options_header(self.headers.get("Content-Type"))
        if self._form is None and content_type == b"multipart/form-data":
            self.multipart_parser = _CaptureMultiPartParser(
                self.headers,
                self.stream(),
                max_upload_bytes=get_capture_upload_settings().max_upload_bytes,
                max_files=max_files,
                max_fields=max_fields,
                max_part_size=max_part_size,
                allowed_fields=self._allowed_fields,
                repeatable_fields=self._repeatable_fields,
            )
            try:
                self._form = await self.multipart_parser.parse()
            except _InvalidMultipartField as exc:
                location = ("body", exc.field) if exc.field is not None else ("body",)
                raise RequestValidationError(
                    [{"loc": location, "type": exc.code, "input": None}],
                ) from None
            except CaptureUploadTooLargeError as exc:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail="capture_upload_too_large",
                ) from exc
            except (ClientDisconnect, MultiPartException, OSError) as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="There was an error parsing the body",
                ) from exc
        return await super()._get_form(
            max_files=max_files,
            max_fields=max_fields,
            max_part_size=max_part_size,
        )


class CaptureUploadRoute(APIRoute):
    """Keep FastAPI form validation while bounding the upload parser's writes."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Wrap only this upload route, downstream of pre-body authentication."""
        handler = super().get_route_handler()
        fields = {param.alias: param.field_info.annotation for param in self.dependant.body_params}
        allowed_fields = frozenset(fields)
        repeatable_fields = frozenset(
            name for name, annotation in fields.items() if _is_repeatable(annotation)
        )

        async def bounded_handler(request: Request) -> Response:
            capture_request = _CaptureRequest(
                request.scope,
                request.receive,
                allowed_fields=allowed_fields,
                repeatable_fields=repeatable_fields,
            )
            try:
                content_type, _ = parse_options_header(capture_request.headers.get("Content-Type"))
                if content_type == b"multipart/form-data":
                    # Validate parser callbacks outside FastAPI's body-error
                    # wrapper, which converts arbitrary exceptions to 400.
                    await capture_request.form()
                return await handler(capture_request)
            finally:
                if capture_request.multipart_parser is not None:
                    capture_request.multipart_parser.close_files()

        return bounded_handler
