"""Packaged native attachment route, bounded to the locally verified file type."""
from pathlib import Path
from .policy import AdapterError

NATIVE_SHA256 = '07f53d6c428eab457438a49909518101cf81d79f03c58faa403303ca68f996c8'
SUPPORTED_FILE_EXTENSIONS = ('.txt', '.pdf', '.zip')


def validate_file_type(path):
    if (not isinstance(path, str) or
            Path(path).suffix.casefold() not in SUPPORTED_FILE_EXTENSIONS):
        raise AdapterError('unverified_file_type',
                           'this release validates only .txt, .pdf, and .zip file attachments')


def send_file(adapter, session_ref, file):
    from .native_driver import NativeAttachmentDriver, VerifiedFile
    from .file_actions import send_file as submit
    import win32process

    if not isinstance(file, dict) or set(file) != {'path', 'name', 'size', 'sha256'}:
        raise AdapterError('invalid_file_descriptor')
    validate_file_type(file['path'])
    tid, pid = win32process.GetWindowThreadProcessId(adapter.hwnd)
    if pid != adapter.pid:
        raise AdapterError('stale_window')
    target = dict(pid=adapter.pid, hwnd=adapter.hwnd, created=adapter.created, tid=tid)
    dll = (Path(__file__).parent / 'native' / NATIVE_SHA256 / 'wxbg_attachment.dll').resolve(strict=True)
    # Rehash in the worker and hold its independent read handle to completion.
    # The gateway also holds its handle while awaiting the guardian response.
    with VerifiedFile(file['path'], expected_sha256=file['sha256'], expected_size=file['size']) as descriptor:
        if descriptor != file:
            raise AdapterError('file_descriptor_changed')
        driver = NativeAttachmentDriver(target, dll, NATIVE_SHA256)
        return submit(adapter, driver, session_ref, descriptor)
