"""PDF 图片压缩工具（保持原始分辨率，消除文字重影）

用法:
    python optimize_pdf.py 输入.pdf 输出.pdf

为什么这样改:
  旧版把 1920px 截图统一 LANCZOS 缩到 1250px 再转 JPEG(q78)，细文字在缩放
  重采样和 JPEG 振铃效应下出现“重影/白边”。本版不再缩小图片，保持原始
  分辨率，文字几何不变；并按内容自适应选择编码：
    - 原本就是 JPEG 的：原样保留，不二次压缩；
    - 平面/线框类 HMI 截图（大面积纯色、锐利文字）：保留无损 PNG/Flate，
      这类图无损往往比 JPEG 更小且文字清晰；
    - 真正照片/复杂渐变：转 JPEG(q85)，且仅当确实更小才替换；
    - 最长边 < 400 的小图标：保持原样。
  带 SMask 透明蒙版的图片，蒙版在原分辨率下无损保留并重新挂接（不再缩放，
  与颜色层完全对齐），全不透明蒙版直接丢弃。
依赖: pymupdf, pillow, pikepdf(可选)
"""
import io, os, sys, time
import pymupdf
from PIL import Image

QUALITY = 85
SMALL_LIMIT = 400
# 仅当 JPEG 比原流小到这个比例才替换，避免无意义的体积/画质交换
REPLACE_RATIO = 0.95


def _jpeg(img):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=QUALITY, optimize=True)
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
                extracted = doc.extract_image(xref)
                old_bytes = extracted["image"]
                ext = extracted["ext"]
                width, height = extracted["width"], extracted["height"]

                # 体积基准必须用 PDF 里实际存储的原始流（xref_stream_raw）；
                # extract_image 会把 PNG 重新压缩得更小，不能作为比较基准。
                old_total = len(doc.xref_stream_raw(xref) or b"")
                smk = doc.xref_get_key(xref, "SMask")
                old_smx = int(smk[1].split()[0]) if smk[0] == "xref" else 0
                if old_smx:
                    old_total += len(doc.xref_stream_raw(old_smx) or b"")

                # 已是 JPEG：保留原流，避免二次压缩产生代际损失
                # 小图标：保持无损，避免 JPEG 破坏平面图形
                if ext == "jpeg" or max(width, height) < SMALL_LIMIT:
                    stats["kept"] += 1
                    continue

                # 无损图：在原分辨率下试编码 JPEG，比较体积
                rgb = Image.open(io.BytesIO(old_bytes)).convert("RGB")
                new_color = _jpeg(rgb)

                # JPEG 没有明显更小：保留原无损流（含原 SMask），文字最清晰
                if len(new_color) >= old_total * REPLACE_RATIO:
                    stats["kept"] += 1
                    continue

                # 处理透明蒙版：原分辨率，不缩放，与颜色层对齐
                new_mask = None
                if old_smx:
                    mpix = pymupdf.Pixmap(doc, old_smx)
                    alpha = Image.frombytes("L", (mpix.width, mpix.height), mpix.samples)
                    if alpha.getextrema()[0] != 255:  # 非全不透明才保留
                        mb = io.BytesIO()
                        alpha.save(mb, "PNG", optimize=True)
                        new_mask = mb.getvalue()

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
