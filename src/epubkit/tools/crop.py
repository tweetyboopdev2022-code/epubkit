#!/usr/bin/env python3
"""Work out a content crop box per book, so scans lose their browser
headers/footers and white borders without the framing jittering page to page."""
import numpy as np
from PIL import Image

WHITE = 242          # pixels at least this bright count as background


def _dark(im):
    return np.asarray(im.convert('L'), dtype=np.uint8) < WHITE


def bands(profile, gap_frac=0.006, thresh=0.0015):
    """Contiguous runs of 'has content', separated by blank gaps."""
    n = len(profile)
    gap = max(3, int(n * gap_frac))
    on = profile > thresh
    out, start, blank = [], None, 0
    for i, v in enumerate(on):
        if v:
            if start is None:
                start = i
            blank = 0
        else:
            if start is not None:
                blank += 1
                if blank >= gap:
                    out.append((start, i - blank + 1))
                    start = None
    if start is not None:
        out.append((start, n))
    return out


def _strip_furniture(bs, n, edge=0.12, thin=0.06):
    """Drop thin bands hugging the top/bottom (or left/right) edge -- these are
    the date/URL strips the flipbook exports leave behind."""
    keep = []
    for a, b in bs:
        thickness = (b - a) / n
        near_edge = (a < edge * n) or (b > (1 - edge) * n)
        if near_edge and thickness < thin:
            continue
        keep.append((a, b))
    return keep or bs


def content_box(im):
    """Bounding box of the real content, ignoring edge furniture."""
    dark = _dark(im)
    H, W = dark.shape
    rp = dark.mean(axis=1)
    vb = _strip_furniture(bands(rp), H)
    if not vb:
        return None
    top, bot = min(b[0] for b in vb), max(b[1] for b in vb)
    sub = dark[top:bot, :]
    cp = sub.mean(axis=0)
    hb = _strip_furniture(bands(cp), W)
    if not hb:
        return None
    left, right = min(b[0] for b in hb), max(b[1] for b in hb)
    return left, top, right, bot


def is_blank(im, thresh=0.005):
    g = np.asarray(im.convert('L').resize((200, 260)), dtype=np.uint8)
    return float((g < WHITE).mean()) < thresh


def book_crop(paths, sample=28, pad=0.006, skip_first=1):
    """One representative crop box for the book, from its interior pages."""
    body = paths[skip_first:] or paths
    if len(body) > sample:
        step = len(body) / sample
        sel = [body[int(i * step)] for i in range(sample)]
    else:
        sel = body
    boxes, W, H = [], None, None
    for p in sel:
        im = Image.open(p)
        W, H = im.size
        if is_blank(im):
            continue
        b = content_box(im)
        if b:
            boxes.append(b)
    if not boxes:
        return None, (W, H)
    a = np.array(boxes, dtype=float)
    l, t = np.percentile(a[:, 0], 10), np.percentile(a[:, 1], 10)
    r, bo = np.percentile(a[:, 2], 90), np.percentile(a[:, 3], 90)
    px, py = int(W * pad), int(H * pad)
    box = (max(0, int(l) - px), max(0, int(t) - py),
           min(W, int(r) + px), min(H, int(bo) + py))
    if (box[2] - box[0]) > 0.975 * W and (box[3] - box[1]) > 0.975 * H:
        return None, (W, H)
    return box, (W, H)


def crop_page(im, box, pad=0.006):
    """Crop to the book box widened to include anything this page has outside it,
    so a full-bleed cover is never clipped."""
    if box is None:
        return im
    W, H = im.size
    own = content_box(im)
    if own is None:
        return im.crop(box)
    px, py = int(W * pad), int(H * pad)
    b = (max(0, min(box[0], own[0] - px)), max(0, min(box[1], own[1] - py)),
         min(W, max(box[2], own[2] + px)), min(H, max(box[3], own[3] + py)))
    return im.crop(b)


def gutter_split(im, centre_frac=0.035, ink_thresh=0.02, colour_thresh=26,
                 ink_veto=0.10):
    """Decide whether a two-page spread can be cut down the middle.

    True when the centre strip carries little linework, or the two halves have
    distinct background tones and nothing substantial is drawn over the gutter.
    A lot of ink in the centre strip vetoes the split outright -- that is an
    illustration (a school bus, say) running across both pages.
    """
    g = np.asarray(im.convert('L'), dtype=np.uint8)
    rgb = np.asarray(im.convert('RGB'), dtype=np.int16)
    H, W = g.shape
    c = W // 2
    half = max(3, int(W * centre_frac / 2))
    strip = g[:, c - half:c + half]
    strip_ink = float((strip < 110).mean())
    lo = rgb[:, int(W * 0.05):int(W * 0.20)].reshape(-1, 3).mean(axis=0)
    ro = rgb[:, int(W * 0.80):int(W * 0.95)].reshape(-1, 3).mean(axis=0)
    colour_gap = float(np.abs(lo - ro).max())
    split = strip_ink < ink_thresh or (colour_gap > colour_thresh and strip_ink < ink_veto)
    return bool(split), strip_ink, colour_gap
