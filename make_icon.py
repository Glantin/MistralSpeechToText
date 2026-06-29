"""Genere l'icone de l'app : micro blanc sur carre arrondi en degrade orange.

Outil de dev (pas embarque dans l'app). Rend un PNG 1024 px via AppKit, puis
construit l'iconset multi-resolutions et `assets/AppIcon.icns` (consomme par
MistralSTT.spec lors du build).

    uv run python make_icon.py
"""

import os
import subprocess

from AppKit import (
    NSBezierPath,
    NSBitmapImageFileTypePNG,
    NSBitmapImageRep,
    NSColor,
    NSCompositingOperationSourceAtop,
    NSCompositingOperationSourceOver,
    NSDeviceRGBColorSpace,
    NSGradient,
    NSGraphicsContext,
    NSImage,
    NSImageSymbolConfiguration,
    NSMakeRect,
    NSRectFillUsingOperation,
    NSZeroRect,
)

ASSETS = "assets"
MASTER = os.path.join(ASSETS, "AppIcon_master.png")
ICONSET = os.path.join(ASSETS, "AppIcon.iconset")
ICNS = os.path.join(ASSETS, "AppIcon.icns")
S = 1024


def _white_mic(side: float) -> NSImage:
    """SF Symbol mic.fill rendu en blanc plein (les symboles sont des templates)."""
    sym = NSImage.imageWithSystemSymbolName_accessibilityDescription_("mic.fill", None)
    conf = NSImageSymbolConfiguration.configurationWithPointSize_weight_scale_(
        side, 0.0, 3  # poids regular, echelle large
    )
    sym = sym.imageWithSymbolConfiguration_(conf)
    size = sym.size()
    out = NSImage.alloc().initWithSize_(size)
    out.lockFocus()
    rect = NSMakeRect(0, 0, size.width, size.height)
    sym.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
        rect, NSZeroRect, NSCompositingOperationSourceOver, 1.0, True, None
    )
    NSColor.whiteColor().set()
    NSRectFillUsingOperation(rect, NSCompositingOperationSourceAtop)
    out.unlockFocus()
    return out


def render_master() -> None:
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, S, S, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0
    )
    ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(ctx)

    # Carre arrondi (style "squircle" macOS) avec degrade orange -> ambre.
    inset = S * 0.085
    rect = NSMakeRect(inset, inset, S - 2 * inset, S - 2 * inset)
    radius = (S - 2 * inset) * 0.235
    path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, radius, radius)
    amber = NSColor.colorWithSRGBRed_green_blue_alpha_(1.00, 0.66, 0.07, 1.0)
    orange = NSColor.colorWithSRGBRed_green_blue_alpha_(0.95, 0.34, 0.12, 1.0)
    grad = NSGradient.alloc().initWithStartingColor_endingColor_(orange, amber)
    grad.drawInBezierPath_angle_(path, 90.0)  # orange en bas, ambre en haut

    # Micro blanc centre.
    mic = _white_mic(S * 0.46)
    msize = mic.size()
    target_h = S * 0.52
    scale = target_h / msize.height
    target_w = msize.width * scale
    mx = (S - target_w) / 2.0
    my = (S - target_h) / 2.0
    mic.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
        NSMakeRect(mx, my, target_w, target_h),
        NSZeroRect,
        NSCompositingOperationSourceOver,
        1.0,
        True,
        None,
    )

    NSGraphicsContext.restoreGraphicsState()

    os.makedirs(ASSETS, exist_ok=True)
    data = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
    data.writeToFile_atomically_(MASTER, True)


def build_icns() -> None:
    os.makedirs(ICONSET, exist_ok=True)
    specs = [
        (16, "icon_16x16.png"),
        (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"),
        (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
        (1024, "icon_512x512@2x.png"),
    ]
    for px, name in specs:
        subprocess.run(
            ["sips", "-z", str(px), str(px), MASTER, "--out", os.path.join(ICONSET, name)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    subprocess.run(["iconutil", "-c", "icns", ICONSET, "-o", ICNS], check=True)


if __name__ == "__main__":
    render_master()
    build_icns()
    print(f"OK -> {ICNS}")
