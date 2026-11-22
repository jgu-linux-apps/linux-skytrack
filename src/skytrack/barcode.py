"""Read 2D codes (Aztec, PDF417, QR, Data Matrix) with the ZXing-C++ library already on Ubuntu (libzxing4,
pulled in by GStreamer / Qt) through its C API and ctypes — no Python package needed. zbar cannot read
Aztec, which most airlines use on mobile boarding passes.

Sources: raw grey / RGB(A) pixels (camera frames), image files, PDFs (rendered with pdftoppm from
poppler-utils) and Gdk textures (clipboard / drag and drop).
"""
import ctypes
import ctypes.util
import os
import shutil
import subprocess
import tempfile

FMT_LUM = 0x01000000
FMT_RGB = 0x03000102
FMT_RGBA = 0x04000102
FMT_BGRA = 0x04020100

_lib = None
_tried = False


def _load():
    global _lib, _tried
    if _tried:
        return _lib
    _tried = True
    names = [os.environ.get("SKYTRACK_ZXING_LIB", ""), "libZXing.so.4", "libZXing.so.3",
             ctypes.util.find_library("ZXing") or ""]
    for n in filter(None, names):
        try:
            lib = ctypes.CDLL(n)
            lib.ZXing_ReadBarcodes            # C API present (zxing-cpp 2.2+)
        except (OSError, AttributeError):
            continue
        vp, i, cp = ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p
        lib.ZXing_ImageView_new.restype = vp
        lib.ZXing_ImageView_new.argtypes = [vp, i, i, ctypes.c_uint, i, i]
        lib.ZXing_ImageView_delete.argtypes = [vp]
        lib.ZXing_ReaderOptions_new.restype = vp
        lib.ZXing_ReaderOptions_delete.argtypes = [vp]
        for f in ("ZXing_ReaderOptions_setTryHarder", "ZXing_ReaderOptions_setTryRotate",
                  "ZXing_ReaderOptions_setTryInvert", "ZXing_ReaderOptions_setTryDownscale"):
            getattr(lib, f).argtypes = [vp, ctypes.c_bool]
        lib.ZXing_ReadBarcodes.restype = vp
        lib.ZXing_ReadBarcodes.argtypes = [vp, vp]
        lib.ZXing_Barcodes_size.restype = i
        lib.ZXing_Barcodes_size.argtypes = [vp]
        lib.ZXing_Barcodes_at.restype = vp
        lib.ZXing_Barcodes_at.argtypes = [vp, i]
        lib.ZXing_Barcodes_delete.argtypes = [vp]
        lib.ZXing_Barcode_text.restype = vp          # char*, freed with ZXing_free
        lib.ZXing_Barcode_text.argtypes = [vp]
        lib.ZXing_free.argtypes = [vp]
        lib.ZXing_LastErrorMsg.restype = vp
        # writer (tests and the "how it works" sample)
        lib.ZXing_BarcodeFormatFromString.restype = ctypes.c_uint
        lib.ZXing_BarcodeFormatFromString.argtypes = [cp]
        lib.ZXing_CreatorOptions_new.restype = vp
        lib.ZXing_CreatorOptions_new.argtypes = [ctypes.c_uint]
        lib.ZXing_CreatorOptions_delete.argtypes = [vp]
        lib.ZXing_CreateBarcodeFromText.restype = vp
        lib.ZXing_CreateBarcodeFromText.argtypes = [cp, i, vp]
        lib.ZXing_Barcode_delete.argtypes = [vp]
        lib.ZXing_WriterOptions_new.restype = vp
        lib.ZXing_WriterOptions_setScale.argtypes = [vp, i]
        lib.ZXing_WriterOptions_delete.argtypes = [vp]
        lib.ZXing_WriteBarcodeToImage.restype = vp
        lib.ZXing_WriteBarcodeToImage.argtypes = [vp, vp]
        lib.ZXing_Image_data.restype = ctypes.POINTER(ctypes.c_uint8)
        lib.ZXing_Image_data.argtypes = [vp]
        lib.ZXing_Image_width.restype = i
        lib.ZXing_Image_width.argtypes = [vp]
        lib.ZXing_Image_height.restype = i
        lib.ZXing_Image_height.argtypes = [vp]
        lib.ZXing_Image_delete.argtypes = [vp]
        _lib = lib
        break
    return _lib


def available():
    return _load() is not None


def pdf_available():
    return shutil.which("pdftoppm") is not None


def decode_pixels(data, width, height, fmt=FMT_LUM, row_stride=0, pix_stride=0, harder=True):
    """Texts of all codes found in a frame (list of str)."""
    lib = _load()
    if lib is None or not data or width <= 0 or height <= 0:
        return []
    buf = (ctypes.c_uint8 * len(data)).from_buffer_copy(data) if not isinstance(data, ctypes.Array) else data
    iv = lib.ZXing_ImageView_new(buf, width, height, fmt, row_stride, pix_stride)
    if not iv:
        return []
    opts = lib.ZXing_ReaderOptions_new()
    lib.ZXing_ReaderOptions_setTryHarder(opts, harder)
    lib.ZXing_ReaderOptions_setTryRotate(opts, True)
    lib.ZXing_ReaderOptions_setTryInvert(opts, True)       # dark-mode passes in Wallet
    lib.ZXing_ReaderOptions_setTryDownscale(opts, True)
    out = []
    try:
        res = lib.ZXing_ReadBarcodes(iv, opts)
        if res:
            for k in range(lib.ZXing_Barcodes_size(res)):
                p = lib.ZXing_Barcode_text(lib.ZXing_Barcodes_at(res, k))
                if p:
                    out.append(ctypes.string_at(p).decode("utf-8", "replace"))
                    lib.ZXing_free(p)
            lib.ZXing_Barcodes_delete(res)
    finally:
        lib.ZXing_ReaderOptions_delete(opts)
        lib.ZXing_ImageView_delete(iv)
    return out


def decode_pixbuf(pb):
    fmt = FMT_RGBA if pb.get_has_alpha() else FMT_RGB
    return decode_pixels(pb.get_pixels(), pb.get_width(), pb.get_height(), fmt,
                         pb.get_rowstride(), pb.get_n_channels())


def decode_texture(texture):
    """Gdk.Texture (clipboard, drag and drop) -> texts. download() gives Cairo ARGB32 = BGRA in memory."""
    w, h = texture.get_width(), texture.get_height()
    stride = w * 4
    buf = bytearray(stride * h)
    texture.download(buf, stride)
    return decode_pixels(bytes(buf), w, h, FMT_BGRA, stride, 4)


def decode_image_file(path):
    from gi.repository import GdkPixbuf
    pb = GdkPixbuf.Pixbuf.new_from_file(path)
    pb = pb.apply_embedded_orientation() or pb
    found = decode_pixbuf(pb)
    if not found and max(pb.get_width(), pb.get_height()) > 2400:
        # big phone photos: a smaller copy is sometimes read better
        k = 2000 / max(pb.get_width(), pb.get_height())
        small = pb.scale_simple(int(pb.get_width() * k), int(pb.get_height() * k), GdkPixbuf.InterpType.BILINEAR)
        found = decode_pixbuf(small)
    return found


def decode_pdf(path, pages=3):
    """Render the first pages (200 dpi, then 300 dpi) and read them."""
    exe = shutil.which("pdftoppm")
    if not exe:
        raise FileNotFoundError("pdftoppm")
    with tempfile.TemporaryDirectory(prefix="skytrack-pdf-") as tmp:
        for dpi in (200, 300):
            prefix = os.path.join(tmp, f"p{dpi}")
            subprocess.run([exe, "-r", str(dpi), "-f", "1", "-l", str(pages), "-png", path, prefix],
                           check=False, capture_output=True, timeout=60)
            found = []
            for name in sorted(os.listdir(tmp)):
                if name.startswith(f"p{dpi}") and name.endswith(".png"):
                    found += decode_image_file(os.path.join(tmp, name))
            if found:
                return found
    return []


def decode_file(path):
    if path.lower().endswith(".pdf"):
        return decode_pdf(path)
    with open(path, "rb") as f:
        if f.read(5) == b"%PDF-":
            return decode_pdf(path)
    return decode_image_file(path)


def encode(text, fmt="Aztec", scale=4):
    """(grey bytes, width, height) of a code — for tests."""
    lib = _load()
    if lib is None:
        return None
    co = lib.ZXing_CreatorOptions_new(lib.ZXing_BarcodeFormatFromString(fmt.encode()))
    raw = text.encode()
    bc = lib.ZXing_CreateBarcodeFromText(raw, len(raw), co)
    lib.ZXing_CreatorOptions_delete(co)
    if not bc:
        return None
    wo = lib.ZXing_WriterOptions_new()
    lib.ZXing_WriterOptions_setScale(wo, scale)
    img = lib.ZXing_WriteBarcodeToImage(bc, wo)
    lib.ZXing_WriterOptions_delete(wo)
    lib.ZXing_Barcode_delete(bc)
    if not img:
        return None
    w, h = lib.ZXing_Image_width(img), lib.ZXing_Image_height(img)
    data = ctypes.string_at(lib.ZXing_Image_data(img), w * h)
    lib.ZXing_Image_delete(img)
    return data, w, h
