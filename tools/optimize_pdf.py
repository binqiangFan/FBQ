"""PDF 图片压缩工具（保留透明蒙版）

用法:
    python optimize_pdf.py 输入.pdf 输出.pdf

做法:
  - 最长边超过 1250 的图片按 LANCZOS 缩小，颜色层转 JPEG(q78)；
  - 带 SMask 透明蒙版的图片，蒙版按 PNG(Flate) 无损保留并重新挂接，
    全不透明的蒙版直接丢弃；
  - 最长边 < 400 的小图标保持原样，避免 JPEG 破坏平面图形；
  - 新体积不足原体积 90% 时不替换；
  - 保存后若安装了 pikepdf，再做一次线性化（Fast Web View）。
依赖: pymupdf, pillow, pikepdf(可选)
"""
import io, os, sys, time
import pymupdf
from PIL import Image

MAX_SIDE = 1250
QUALITY = 78
SMALL_LIMIT = 400


def _jpeg(img):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=QUALITY, optimize=True)
    return buf.getvalue()


def _png(img):
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def optimize(src, dst):
    doc = pymupdf.open(src)
    tmp = doc.new_page()
    temp_idx = doc.page_count - 1
    seen = set()
    stats = {"replaced": 0, "kept": 0, "fail": 0}
    t0 = time.time()

    for pno in range(doc.page_count):
        if pno == temp_idx:
            continue
        page = doc[pno]
        for info in page.get_images(full=True):
            xref = info[0]
            if xref in seen:
                continue
            seen.add(xref)
            try:
                old_total = len(doc.xref_stream_raw(xref) or b"")
                smk = doc.xref_get_key(xref, "SMask")
                old_smx = int(smk[1].split()[0]) if smk[0] == "xref" else 0
                if old_smx:
                    old_total += len(doc.xref_stream_raw(old_smx) or b"")

                cpix = pymupdf.Pixmap(doc, xref)
                if cpix.n - cpix.alpha > 3:
                    cpix = pymupdf.Pixmap(pymupdf.csRGB, cpix)
                elif cpix.alpha:
                    cpix = pymupdf.Pixmap(cpix, 0)
                rgb = Image.frombytes("RGB", (cpix.width, cpix.height), cpix.samples)

                if max(rgb.size) < SMALL_LIMIT:
                    stats["kept"] += 1
                    continue

                alpha = None
                if old_smx:
                    mpix = pymupdf.Pixmap(doc, old_smx)
                    alpha = Image.frombytes("L", (mpix.width, mpix.height), mpix.samples)
                    if alpha.getextrema()[0] == 255:
                        alpha = None

                if max(rgb.size) > MAX_SIDE:
                    r = MAX_SIDE / max(rgb.size)
                    rgb = rgb.resize((max(1, int(rgb.width * r)),
                                      max(1, int(rgb.height * r))), Image.LANCZOS)
                    if alpha is not None:
                        alpha = alpha.resize(rgb.size, Image.LANCZOS)

                new_color = _jpeg(rgb)
                new_mask = _png(alpha) if alpha is not None else None
                new_total = len(new_color) + (len(new_mask) if new_mask else 0)

                if new_total >= old_total * 0.9:
                    stats["kept"] += 1
                    continue

                page.replace_image(xref, stream=new_color)
                if new_mask:
                    mx = tmp.insert_image(pymupdf.Rect(0, 0, 10, 10), stream=new_mask)
                    # 手机端 PDF 引擎要求 SMask 必须是 DeviceGray，
                    # insert_image 偶尔会挂 ICCBased 灰配置，会导致整图不显示
                    doc.xref_set_key(mx, "ColorSpace", "/DeviceGray")
                    doc.xref_set_key(xref, "SMask", "%d 0 R" % mx)
                else:
                    doc.xref_set_key(xref, "SMask", "null")
                stats["replaced"] += 1
            except Exception as e:
                stats["fail"] += 1
                print("fail xref %s: %r" % (xref, e))

    doc.delete_page(temp_idx)
    doc.save(dst, garbage=4, deflate=True, clean=True)
    doc.close()
    print("统计:", stats, " 用时 %.1fs" % (time.time() - t0))


def linearize(path):
    try:
        import pikepdf
    except ImportError:
        print("未安装 pikepdf，跳过线性化。")
        return
    tmp_path = path + ".tmp"
    with pikepdf.open(path) as pdf:
        pdf.save(tmp_path, linearize=True,
                 object_stream_mode=pikepdf.ObjectStreamMode.generate,
                 compress_streams=True)
    os.replace(tmp_path, path)
    raw = open(path, "rb").read(2048)
    print("线性化完成: %s" % (b"/Linearized" in raw))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("用法: python optimize_pdf.py 输入.pdf 输出.pdf")
    optimize(sys.argv[1], sys.argv[2])
    linearize(sys.argv[2])
    print("输出: %s (%.2f MB)" % (sys.argv[2], os.path.getsize(sys.argv[2]) / 1048576))
