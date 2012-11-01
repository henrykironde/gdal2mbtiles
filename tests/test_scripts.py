# -*- coding: utf-8 -*-

from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import os
from subprocess import CalledProcessError, check_call
import sys
from tempfile import NamedTemporaryFile
import unittest

from gdal2mbtiles.mbtiles import MBTiles


__dir__ = os.path.dirname(__file__)


class TestGdal2mbtilesScript(unittest.TestCase):
    def setUp(self):
        self.repo_dir = os.path.join(__dir__, os.path.pardir)
        self.scripts_dir = os.path.join(self.repo_dir, 'scripts')
        self.script = os.path.join(self.scripts_dir, 'gdal2mbtiles')

        self.environ = os.environ.copy()

        # Make sure you can get to the local gdal2mbtiles module
        pythonpath = self.environ.get('PYTHONPATH', [])
        if pythonpath:
            pythonpath = pythonpath.split(os.path.pathsep)
        pythonpath = os.path.pathsep.join([self.repo_dir] + pythonpath)
        self.environ['PYTHONPATH'] = pythonpath

        self.inputfile = os.path.join(__dir__, 'upsampling.tif')
        self.rgbfile = os.path.join(__dir__, 'bluemarble.tif')

    def test_simple(self):
        with NamedTemporaryFile(suffix='.mbtiles') as output:
            command = [sys.executable, self.script, self.inputfile, output.name]
            check_call(command, env=self.environ)
            with MBTiles(output.name) as mbtiles:
                # 4×4 at resolution 2
                cursor = mbtiles._conn.execute('SELECT COUNT(*) FROM tiles')
                self.assertEqual(cursor.fetchone(), (1,))

    def test_metadata(self):
        with NamedTemporaryFile(suffix='.mbtiles') as output:
            command = [sys.executable, self.script, self.inputfile, output.name]
            check_call(command, env=self.environ)

            # Dataset (upsampling.tif) bounds in EPSG:4326
            dataset_bounds = '-180.0,-90.0,180.0,90.0'

            with MBTiles(output.name) as mbtiles:
                # Default metadata
                cursor = mbtiles._conn.execute('SELECT * FROM metadata')
                self.assertTrue(dict(cursor.fetchall()),
                                dict(name=os.path.basename(self.inputfile),
                                     description='',
                                     format='png',
                                     type='overlay',
                                     version='1.0.0',
                                     bounds=dataset_bounds))

            command = [sys.executable, self.script,
                       '--name', 'test',
                       '--description', 'Unit test',
                       '--format', 'jpg',
                       '--layer-type', 'baselayer',
                       '--version', '2.0.1',
                       self.inputfile, output.name]
            check_call(command, env=self.environ)
            with MBTiles(output.name) as mbtiles:
                # Default metadata
                cursor = mbtiles._conn.execute('SELECT * FROM metadata')
                self.assertEqual(dict(cursor.fetchall()),
                                 dict(name='test',
                                      description='Unit test',
                                      format='jpg',
                                      type='baselayer',
                                      version='2.0.1',
                                      bounds=dataset_bounds))

    def test_warp(self):
        null = open('/dev/null', 'rw')

        with NamedTemporaryFile(suffix='.mbtiles') as output:
            # Valid
            command = [sys.executable, self.script,
                       '--spatial-reference', '4326',
                       '--resampling', 'bilinear',
                       self.rgbfile, output.name]
            check_call(command, env=self.environ)

            # Invalid spatial reference
            command = [sys.executable, self.script,
                       '--spatial-reference', '9999',
                       self.inputfile, output.name]
            self.assertRaises(CalledProcessError,
                              check_call, command, env=self.environ,
                              stderr=null)

            # Invalid resampling
            command = [sys.executable, self.script,
                       '--resampling', 'montecarlo',
                       self.inputfile, output.name]
            self.assertRaises(CalledProcessError,
                              check_call, command, env=self.environ,
                              stderr=null)

    def test_render(self):
        null = open('/dev/null', 'rw')

        with NamedTemporaryFile(suffix='.mbtiles') as output:
            # Valid
            command = [sys.executable, self.script,
                       '--min-resolution', '1',
                       '--max-resolution', '3',
                       self.rgbfile, output.name]
            check_call(command, env=self.environ)
            with MBTiles(output.name) as mbtiles:
                cursor = mbtiles._conn.execute(
                    """
                    SELECT zoom_level, COUNT(*) FROM tiles
                    GROUP BY zoom_level
                    """
                )
                self.assertEqual(
                    dict(cursor.fetchall()),
                    {1: 4,   # 2×2 at resolution 1
                     2: 16,  # 4×4 at resolution 2
                     3: 64}  # 8×8 at resolution 3
                )

            # Min resolution greater than input resolution
            command = [sys.executable, self.script,
                       '--min-resolution', '3',
                       self.inputfile, output.name]
            self.assertRaises(
                CalledProcessError,
                check_call, command, env=self.environ, stderr=null
            )

            # Min resolution greater than max resolution
            command = [sys.executable, self.script,
                       '--min-resolution', '2',
                       '--max-resolution', '1',
                       self.inputfile, output.name]
            self.assertRaises(
                CalledProcessError,
                check_call, command, env=self.environ, stderr=null
            )

            # Max resolution less than input resolution
            command = [sys.executable, self.script,
                       '--max-resolution', '0',
                       self.rgbfile, output.name]
            self.assertRaises(
                CalledProcessError,
                check_call, command, env=self.environ, stderr=null
            )