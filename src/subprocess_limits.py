"""How long each external renderer may run before its call is abandoned.

Every Ghostscript and Inkscape call passes one of these as ``timeout=``, so a stalled
child (a hung font lookup, a renderer waiting on stdin) ends the step with an error that
names it, instead of leaving a batch waiting forever. The bounds are far past a healthy
run on a large illustration, not tuned to a typical one.
"""

# One SVG rasterised or exported by Inkscape: seconds for typical artwork, a minute or two
# at 300 dpi for a dense page.
INKSCAPE_RENDER_TIMEOUT_S = 300

# One page of a PDF rasterised by Ghostscript (the soft-proof PNG, the ink-coverage raster,
# the render check): seconds for a page.
GHOSTSCRIPT_PAGE_TIMEOUT_S = 300

# A whole book PDF converted RGB to CMYK, with every page transformed through the ICC
# profile: minutes for a long book.
GHOSTSCRIPT_CONVERT_TIMEOUT_S = 1800
