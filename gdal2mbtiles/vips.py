# -*- coding: utf-8 -*-

from __future__ import absolute_import, division

from math import ceil
import os

import vipsCC.VImage

from .constants import TILE_SIDE
from .gdal import Dataset
from .pool import Pool
from .types import XY
from .utils import get_hasher, makedirs, tempenv


# Process pool
pool = Pool(processes=None)


class VImage(vipsCC.VImage.VImage):
    def __init__(self, *args, **kwargs):
        super(VImage, self).__init__(*args, **kwargs)

    @classmethod
    def new_rgba(cls, width, height):
        """Creates a new transparent RGBA image sized width × height."""
        bands = 4                  # RGBA
        bandfmt = cls.FMTUCHAR     # 8-bit unsigned
        coding = cls.NOCODING      # No coding and no compression
        _type = cls.sRGB
        xres, yres = 2.835, 2.835  # Arbitrary 600 dpi
        xo, yo = 0, 0

        image = cls("", "p")       # Working buffer
        image.initdesc(width, height, bands, bandfmt, coding, _type,
                       xres, yres, xo, yo)
        return image

    @classmethod
    def from_vimage(cls, other):
        """Creates a new image from another VImage."""
        new = cls()
        new.__dict__.update(other.__dict__)
        return new

    @classmethod
    def disable_warnings(cls):
        """Context manager to disable VIPS warnings."""
        return tempenv('IM_WARNING', '0')

    def extract_area(self, left, top, width, height):
        return self.from_vimage(
            super(VImage, self).extract_area(left, top, width, height)
        )

    def stretch(self, xscale, yscale):
        """
        Returns a new VImage that has been stretched by `xscale` and `yscale`.

        xscale: floating point scaling value for image
        yscale: floating point scaling value for image
        """
        # Stretch by aligning the centers of the input and output images.
        #
        # See the following blog post, written by the VIPS people:
        # http://libvips.blogspot.ca/2011/12/task-of-day-resize-image-with-align.html
        #
        # This is the image size convention which is ideal for expanding the
        # number of pixels in each direction by an exact fraction (with box
        # filtering, for example). With this image size convention, there is no
        # extrapolation near the boundary when enlarging. Instead of aligning
        # the outer corners, we align the centers of the corner pixels.

        if xscale < 1.0:
            raise ValueError(
                'xscale {0!r} cannot be less than 1.0'.format(xscale)
            )
        if yscale < 1.0:
            raise ValueError(
                'yscale {0!r} cannot be less than 1.0'.format(yscale)
            )

        # The centers of the corners of input.img are located at:
        #     (0,0), (0,m), (n,0) and (n,m).
        # The centers of output.img are located at:
        #     (0,0), (0,M), (N,0) and (N,M).
        output_width = N = int(self.Xsize() * xscale)
        output_height = M = int(self.Ysize() * yscale)

        # The affine transformation that sends each input corner to the
        # corresponding output corner is:
        #     X = ((N-1)/(n-1)) x
        #     Y = ((M-1)/(m-1)) y
        #
        # Use the transformation matrix:
        #     [[(N-1)/(n-1),           0],
        #      [          0, (M-1)/(m-1)]]
        a = (N - 1) / (self.Xsize() - 1)
        b = 0
        c = 0
        d = (M - 1) / (self.Ysize() - 1)

        # Align the centers, because X and Y have no constant term.
        offset_x = 0
        offset_y = 0

        # No translation, so top-left corners match.
        output_x, output_y = 0, 0

        return self.from_vimage(
            self.affine(a, b, c, d, offset_x, offset_y,
                        output_x, output_y, output_width, output_height)
        )

    def shrink(self, xscale, yscale):
        """
        Returns a new VImage that has been shrunk by `xscale` and `yscale`.

        xscale: floating point scaling value for image
        yscale: floating point scaling value for image
        """
        # Shrink by aligning the corners of the input and output images.
        #
        # See the following blog post, written by the VIPS people:
        # http://libvips.blogspot.ca/2011/12/task-of-day-resize-image-with-align.html
        #
        # This is the image size convention which is ideal for reducing the
        # number of pixels in each direction by an exact fraction (with box
        # filtering, for example). With this convention, there is no
        # extrapolation near the boundary when downsampling.

        if not 0.0 < xscale <= 1.0:
            raise ValueError(
                'xscale {0!r} be between 0.0 and 1.0'.format(xscale)
            )
        if not 0.0 < yscale <= 1.0:
            raise ValueError(
                'yscale {0!r} be between 0.0 and 1.0'.format(yscale)
            )

        # The corners of input.img are located at:
        #     (-.5,-.5), (-.5,m-.5), (n-.5,-.5) and (n-.5,m-.5).
        # The corners of output.img are located at:
        #     (-.5,-.5), (-.5,M-.5), (N-.5,-.5) and (N-.5,M-.5).

        output_width = int(self.Xsize() * xscale)
        output_height = int(self.Ysize() * yscale)

        # The affine transformation that sends each input corner to the
        # corresponding output corner is:
        #     X = (M / m) * x + (M / m - 1) / 2
        #     Y = (N / n) * y + (N / n - 1) / 2
        #
        # Since M = m * xscale and N = n * yscale
        #     X = xscale * x + (xscale - 1) / 2
        #     Y = yscale * y + (yscale - 1) / 2
        #
        # Use the transformation matrix:
        #     [[xscale,      0],
        #      [     0, yscale]]
        a, b, c, d = xscale, 0, 0, yscale

        # Align the corners with the constant term of X and Y
        offset_x = (a - 1) / 2
        offset_y = (d - 1) / 2

        # No translation, so top-left corners match.
        output_x, output_y = 0, 0

        return self.from_vimage(
            self.affine(a, b, c, d, offset_x, offset_y,
                        output_x, output_y, output_width, output_height)
        )

    def tms_align(self, tile_width, tile_height, offset):
        """
        Pads and aligns the VIPS Image object to the TMS grid.

        tile_width: Number of pixels for each tile
        tile_height: Number of pixels for each tile
        offset: TMS offset for the lower-left tile
        """
        _type = 0               # Transparent

        # Pixel offset from top-left of the aligned image.
        #
        # The y value needs to be converted from the lower-left corner to the
        # top-left corner.
        x = int(round(offset.x * tile_width)) % tile_width
        y = int(round(self.Ysize() - offset.y * tile_height)) % tile_height

        # Number of tiles for the aligned image, rounded up to provide
        # right and bottom borders.
        tiles_x = ceil((self.Xsize() + x / 2) / tile_width)
        tiles_y = ceil((self.Ysize() + y / 2) / tile_height)

        # Pixel width and height for the aligned image.
        width = int(tiles_x * tile_width)
        height = int(tiles_y * tile_height)

        if width == self.Xsize() and height == self.Ysize():
            # No change
            assert x == y == 0
            return self

        # Resize
        return self.from_vimage(self.embed(_type, x, y, width, height))


class TmsBase(object):
    """Base class for an image in TMS space."""

    def __init__(self, image, outputdir, offset, resolution=None, hasher=None):
        """
        image: gdal2mbtiles.vips.VImage
        outputdir: Output directory for TMS tiles in PNG format
        offset: TMS offset for the lower-left tile
        resolution: Resolution for the image.
                    If None, filenames are in the format
                        ``{tms_x}-{tms_y}-{image_hash}.png``.
                    If an integer, filenames are in the format
                        ``{tms_z}/{tms_x}-{tms_y}-{image_hash}.png``.
        hasher: Hashing function to use for image data.
        """
        self.image = image
        self.outputdir = outputdir
        self.offset = offset
        self.resolution = resolution

        if hasher is None:
            hasher = get_hasher()
        self.hasher = hasher

    def _render_png(self, filename):
        """Helper method to write a VIPS image to filename."""
        return self.image.vips2png(filename)

    @property
    def image_width(self):
        """Returns the width of self.image in pixels."""
        return self.image.Xsize()

    @property
    def image_height(self):
        """Returns the height of self.image in pixels."""
        return self.image.Ysize()


class TmsTile(TmsBase):
    """Represents a single tile in TMS co-ordinates."""

    def get_hash(self):
        """Returns the image content hash."""
        return self.hasher(self.image.tobuffer())

    def create_symlink(self, source, filepath):
        """Creates a relative symlink from filepath to source."""
        absfilepath = os.path.join(self.outputdir, filepath)
        abssourcepath = os.path.join(self.outputdir, source)
        sourcepath = os.path.relpath(abssourcepath,
                                     start=os.path.dirname(absfilepath))
        os.symlink(sourcepath, absfilepath)

    def generate_filepath(self, key, resolution, offset):
        """Returns the filepath, relative to self.outputdir."""
        filename = '{offset.x}-{offset.y}-{key:x}.png'.format(
            offset=offset, key=key
        )
        resolution = '' if resolution is None else str(resolution)
        return os.path.join(resolution, filename)

    def render(self, seen, pool):
        """Renders this tile."""
        hashed = self.get_hash()
        filepath = self.generate_filepath(key=hashed,
                                          resolution=self.resolution,
                                          offset=self.offset)
        if hashed in seen:
            self.create_symlink(source=seen[hashed], filepath=filepath)
        else:
            seen[hashed] = filepath
            pool.apply_async(
                func=self._render_png,
                kwds=dict(filename=os.path.join(self.outputdir, filepath))
            )


class TmsTiles(TmsBase):
    """Represents a set of tiles in TMS co-ordinates."""

    Tile = TmsTile

    def __init__(self, tile_width, tile_height, **kwargs):
        """
        image: gdal2mbtiles.vips.VImage
        outputdir: Output directory for TMS tiles in PNG format
        tile_width: Number of pixels for each tile
        tile_height: Number of pixels for each tile
        offset: TMS offset for the lower-left tile
        resolution: Resolution for the image.
                    If None, filenames are in the format
                        ``{tms_x}-{tms_y}-{image_hash}.png``.
                    If an integer, filenames are in the format
                        ``{tms_z}/{tms_x}-{tms_y}-{image_hash}.png``.
        hasher: Hashing function to use for image data.
        """
        super(TmsTiles, self).__init__(**kwargs)
        self.tile_width = tile_width
        self.tile_height = tile_height

    def _slice(self):
        """Helper function that actually slices tiles. See ``slice``."""
        with self.image.disable_warnings():
            seen = {}
            for y in xrange(0, self.image_height, self.tile_height):
                for x in xrange(0, self.image_width, self.tile_width):
                    out = self.image.extract_area(
                        x, y,                    # left, top offsets
                        self.tile_width, self.tile_height
                    )

                    offset = XY(
                        x=int(x / self.tile_width + self.offset.x),
                        y=int((self.image_height - y) / self.tile_height +
                              self.offset.y - 1)
                    )
                    tile = self.Tile(
                        image=out,
                        outputdir=self.outputdir,
                        offset=offset,
                        resolution=self.resolution,
                        hasher=self.hasher,
                    )
                    tile.render(seen=seen, pool=pool)
            pool.join()

    def slice(self):
        """
        Slices a VIPS image object into TMS tiles in PNG format.

        If a tile duplicates another tile already known to this process, a
        symlink is created instead of rendering the same tile to PNG again.
        """
        # Make self.outputdir and potentially the resolution subdir
        if self.resolution is None:
            makedirs(self.outputdir, ignore_exists=True)
        else:
            makedirs(os.path.join(self.outputdir, str(self.resolution)),
                     ignore_exists=True)

        with self.image.disable_warnings():
            if self.image_width % self.tile_width != 0:
                raise ValueError('image width {0!r} does not contain a whole '
                                 'number of tiles of width {1!r}'.format(
                                     self.image_width, self.tile_width
                                 ))

            if self.image_height % self.tile_height != 0:
                raise ValueError('image height {0!r} does not contain a whole '
                                 'number of tiles of height {1!r}'.format(
                                     self.image_height, self.tile_height
                                 ))

            return self._slice()

    def downsample(self, resolution):
        """
        Downsamples the image by one resolution.

        resolution: Target resolution for the downsampled image.

        Returns a new TmsTiles object containing the downsampled image.
        """
        assert resolution >= 0 and resolution == (self.resolution - 1)

        offset = XY(self.offset.x / 2.0,
                    self.offset.y / 2.0)

        shrunk = self.image.shrink(xscale=0.5, yscale=0.5)
        aligned = shrunk.tms_align(tile_width=self.tile_width,
                                   tile_height=self.tile_height,
                                   offset=offset)

        tiles = self.__class__(image=aligned,
                               outputdir=self.outputdir,
                               tile_width=self.tile_width,
                               tile_height=self.tile_height,
                               offset=XY(int(offset.x), int(offset.y)),
                               resolution=resolution,
                               hasher=self.hasher)
        return tiles

    def upsample(self, resolution):
        """
        Upsample the image.

        resolution: Target resolution for the upsampled image.

        Returns a new TmsTiles object containing the upsampled image.
        """
        # Note: You cannot upsample tile-by-tile because it looks ugly at the
        # boundaries.
        assert resolution > self.resolution

        scale = 2 ** (resolution - self.resolution)

        offset = XY(self.offset.x * scale,
                    self.offset.y * scale)

        stretched = self.image.stretch(xscale=scale, yscale=scale)
        aligned = stretched.tms_align(tile_width=self.tile_width,
                                      tile_height=self.tile_height,
                                      offset=offset)

        tiles = self.__class__(image=aligned,
                               outputdir=self.outputdir,
                               tile_width=self.tile_width,
                               tile_height=self.tile_height,
                               offset=XY(int(offset.x), int(offset.y)),
                               resolution=resolution,
                               hasher=self.hasher)
        return tiles


def image_pyramid(inputfile, outputdir,
                  min_resolution=None, max_resolution=None,
                  hasher=None):
    """
    Slices a GDAL-readable inputfile into a pyramid of PNG tiles.

    inputfile: Filename
    outputdir: The output directory for the PNG tiles.
    min_resolution: Minimum resolution to downsample tiles.
    max_resolution: Maximum resolution to upsample tiles.
    hasher: Hashing function to use for image data.

    Filenames are in the format ``{tms_z}/{tms_x}-{tms_y}-{image_hash}.png``.

    If a tile duplicates another tile already known to this process, a symlink
    may be created instead of rendering the same tile to PNG again.

    If `min_resolution` is None, don't downsample.
    If `max_resolution` is None, don't upsample.
    """
    dataset = Dataset(inputfile)
    lower_left, upper_right = dataset.GetTmsExtents()
    resolution = dataset.GetNativeResolution()

    with VImage.disable_warnings():
        # Native resolution
        tiles = TmsTiles(image=VImage(inputfile),
                         outputdir=outputdir,
                         tile_width=TILE_SIDE, tile_height=TILE_SIDE,
                         offset=lower_left,
                         resolution=resolution,
                         hasher=hasher)
        tiles.slice()

        # Downsampling one zoom level at a time
        if min_resolution is not None:
            for res in reversed(range(min_resolution, resolution)):
                tiles = tiles.downsample(resolution=res)
                tiles.slice()

        # Upsampling one zoom level at a time.
        if max_resolution is not None:
            for res in range(resolution + 1, max_resolution + 1):
                tiles = tiles.upsample(resolution=res)
                tiles.slice()


def image_slice(inputfile, outputdir, hasher=None):
    """
    Slices a GDAL-readable inputfile into PNG tiles.

    inputfile: Filename
    outputdir: The output directory for the PNG tiles.
    hasher: Hashing function to use for image data.

    Filenames are in the format ``{tms_x}-{tms_y}-{image_hash}.png``.

    If a tile duplicates another tile already known to this process, a symlink
    is created instead of rendering the same tile to PNG again.
    """
    dataset = Dataset(inputfile)
    lower_left, upper_right = dataset.GetTmsExtents()

    with VImage.disable_warnings():
        # Native resolution
        native = TmsTiles(image=VImage(inputfile),
                          outputdir=outputdir,
                          tile_width=TILE_SIDE, tile_height=TILE_SIDE,
                          offset=lower_left,
                          hasher=hasher)
        native.slice()
