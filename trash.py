"""Send files to the Windows Recycle Bin so a deleted session can still be restored."""
import ctypes
import sys
from ctypes import wintypes

FO_DELETE = 3
FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 0x4, 0x10, 0x40, 0x400


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [('hwnd', wintypes.HWND), ('wFunc', wintypes.UINT), ('pFrom', wintypes.LPCWSTR),
                ('pTo', wintypes.LPCWSTR), ('fFlags', ctypes.c_ushort), ('fAnyOperationsAborted', wintypes.BOOL),
                ('hNameMappings', ctypes.c_void_p), ('lpszProgressTitle', wintypes.LPCWSTR)]


def send_to_trash(paths):
    paths = [str(p) for p in paths]
    if not paths:
        return
    if sys.platform != 'win32':
        raise OSError('La Papelera de reciclaje solo está disponible en Windows')
    op = SHFILEOPSTRUCTW(wFunc=FO_DELETE, pFrom='\0'.join(paths) + '\0\0',
                         fFlags=FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI)
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if result or op.fAnyOperationsAborted:
        raise OSError(f'Windows no pudo enviar los archivos a la Papelera (código {result})')
