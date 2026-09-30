import cv2
import numpy as np
import os

def load_image(filepath: str) -> np.ndarray:
    if filepath.lower().endswith('.pdf'):
        try:
            import fitz
            doc = fitz.open(filepath)
            page = doc.load_page(0)
            pix = page.get_pixmap(dpi=200)
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
            if pix.n == 4:
                img = cv2.cvtColor(img, cv2.COLOR_RGBA2GRAY)
            elif pix.n == 3:
                img = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
            return img
        except ImportError:
            try:
                from pdf2image import convert_from_path
                images = convert_from_path(filepath, dpi=200)
                img = np.array(images[0])
                if len(img.shape) == 3:
                    img = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
                return img
            except ImportError:
                raise ImportError("Need PyMuPDF or pdf2image to load PDFs")
    else:
        img = cv2.imread(filepath, cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise ValueError(f"Could not read image: {filepath}")
        return img


def load_image_color(filepath: str):
    """Like load_image but keeps colour for images (PDF pages are returned as they are)."""
    if filepath.lower().endswith('.pdf'):
        return load_image(filepath)
    img = cv2.imread(filepath, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"Could not read image: {filepath}")
    return img


def split_pages(filepath: str, out_dir: str) -> list:
    """One image file per page: a PDF is rendered page by page (200 DPI), an image is returned as it is."""
    if not filepath.lower().endswith('.pdf'):
        return [filepath]
    import fitz
    paths = []
    with fitz.open(filepath) as doc:
        for i, page in enumerate(doc):
            out = os.path.join(out_dir, f"{os.path.splitext(os.path.basename(filepath))[0]}_p{i + 1:03d}.png")
            page.get_pixmap(dpi=200, colorspace=fitz.csGRAY).save(out)
            paths.append(out)
    return paths


def page_count(filepath: str) -> int:
    """Pages in a PDF (1 for an image)."""
    if filepath.lower().endswith('.pdf'):
        import fitz
        with fitz.open(filepath) as doc:
            return doc.page_count
    return 1
