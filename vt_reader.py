from qgis.core import *
from GlobalMapTiles import GlobalMercator

import sqlite3
import sys
import os
import site
import importlib
import uuid
import json
import glob
import zlib
from VectorTileHelper import VectorTile

class _GeoTypes:
    POINT = "Point"
    LINE_STRING = "LineString"
    POLYGON = "Polygon"

GeoTypes = _GeoTypes()

class VtReader:  
    geo_types = {
        1: GeoTypes.POINT, 
        2: GeoTypes.LINE_STRING, 
        3: GeoTypes.POLYGON}   

    _extent = 4096    
    directory = os.path.abspath(os.path.dirname(__file__))
    temp_dir = "%s/tmp" % directory
    file_path = "%s/sample data/zurich_switzerland.mbtiles" % directory
    all_features = {}
    
    def __init__(self, iface):
        self.iface = iface
        self.import_libs()
        self._counter = 0
        self._bool = True
        self._mbtile_id = "name"

    def clear_temp_dir(self):
        if not os.path.exists(self.temp_dir):
            os.makedirs(self.temp_dir)
        files = glob.glob("%s/*" % self.temp_dir)
        for f in files:
            os.remove(f)

    def get_empty_feature_collection(self):
        from geojson import FeatureCollection
        crs = { # crs = coordinate reference system
                "type": "name", 
                "properties": { 
                    "name": "urn:ogc:def:crs:EPSG::3857" } }        
        return FeatureCollection([], crs=crs)

    def import_libs(self):        
        site.addsitedir(os.path.join(self.temp_dir, '/ext-libs'))
        self.import_library("google.protobuf")
        self.mvt = self.import_library("mapbox_vector_tile")
        self.geojson = self.import_library("geojson")        

    def import_library(self, lib):
        print "importing: ", lib
        module = importlib.import_module(lib)
        print "import successful"
        return module

    def do_work(self):
        self.clear_temp_dir()
        self.connect_to_db() 
        tile_data_tuples = self.load_tiles_from_db()
        tiles = self.decode_all_tiles(tile_data_tuples)
        self.process_tiles(tiles)

    def connect_to_db(self):
        try:
            self.conn = sqlite3.connect(self.file_path)
            self.conn.row_factory = sqlite3.Row
            print "Successfully connected to db"
        except:
            print "Db connection failed:", sys.exc_info()
            return        

    def load_tiles_from_db(self):
        print "Reading data from db"
        zoom_level = 14
        sql_command = "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles WHERE zoom_level = {} LIMIT 2;".format(zoom_level)
        tile_data_tuples = []
        try:
            cur = self.conn.cursor()
            for row in cur.execute(sql_command):      
                zoom_level = row["zoom_level"]
                tile_col = row["tile_column"]
                tile_row = row["tile_row"]
                binary_data = row["tile_data"]
                tile = VectorTile(zoom_level, tile_col, tile_row)
                tile_data_tuples.append((tile, binary_data))
        except:
            print "Getting data from db failed:", sys.exc_info()
            return
        return tile_data_tuples

    def decode_all_tiles(self, tiles_with_encoded_data):
        tiles = []
        for tile_data_tuple in tiles_with_encoded_data:
            tile = tile_data_tuple[0]
            encoded_data = tile_data_tuple[1]
            tile.decoded_data = self.decode_binary_tile_data(encoded_data)
            tiles.append(tile)
        return tiles

    def process_tiles(self, tiles):
        totalNrTiles = len(tiles)
        for index, tile in enumerate(tiles):          
            self.write_features(tile)
            print "Progress: {0:.1f}%".format(100.0 / totalNrTiles * (index+1))
        self.create_layers_for_geotypes()        

    def decode_binary_tile_data(self, data):
        try:
            # The offset of 32 signals to the zlib header that the gzip header is expected but skipped.
            file_content = zlib.decompress(data, 32 + zlib.MAX_WBITS)
            decoded_data = self.mvt.decode(file_content)                   
        except:
            print "decoding data with mapbox_vector_tile failed", sys.exc_info()
            return
        return decoded_data

    def create_layers_for_geotypes(self):
        root = QgsProject.instance().layerTreeRoot()
        group_name = os.path.splitext(os.path.basename(self.file_path))[0]
        rootGroup = root.addGroup(group_name)
        for feature_path in self.all_features:
            feature_collection = self.all_features[feature_path]                      
            file_src = self.create_unique_file_name()
            with open(file_src, "w") as f:
                json.dump(feature_collection, f)
            if feature_path == "landcover.wood.forest":
                print "landcover.wood.forest: ", feature_collection
            self.add_vector_layer(file_src, feature_path, rootGroup)

    def add_vector_layer(self, json_src, layer_name, layer_target_group):
        # load the created geojson into qgis
        layer = QgsVectorLayer(json_src, layer_name, "ogr")
        QgsMapLayerRegistry.instance().addMapLayer(layer, False)    
        layer_target_group.addLayer(layer)    

    def write_features(self, tile):
        # iterate through all the features of the data and build proper gejson conform objects.
        for layer_name in tile.decoded_data:
            
            print "Handle features of layer: ", layer_name
            tile_features = tile.decoded_data[layer_name]["features"]
            for index, feature in enumerate(tile_features):
                data, geo_type = self.create_geojson_feature(feature, tile)
                if data:
                    feature_class = None
                    feature_subclass = None
                    if "class" in data.properties:
                        feature_class = data.properties["class"]
                        if "subclass" in data.properties:
                            feature_subclass = data.properties["subclass"]
                    
                    if feature_subclass:
                        assert feature_class, "A feature with a subclass should also have a class"

                    feature_path = layer_name;
                    if feature_class:
                        feature_path += "." + feature_class
                        if feature_subclass:
                            feature_path += "." +feature_subclass

                    if not feature_path in self.all_features:
                        self.all_features[feature_path] = self.get_empty_feature_collection()

                    self.all_features[feature_path].features.append(data)
                
                # TODO: remove the break after debugging
                # print "feature: ", feature
                # print "   data: ", data
                #break

    def create_geojson_feature(self, feature, tile):
        """
        Creates a proper GeoJSON feature for the specified feature
        """
        from geojson import Feature, Point, Polygon, LineString

        geo_type = self.geo_types[feature["type"]]
        coordinates = feature["geometry"]
        coordinates = self.map_coordinates_recursive(coordinates, lambda coords: self._calculate_geometry(self, coords, tile))

        if geo_type == GeoTypes.POINT:
            # Due to mercator_geometrys nature, the point will be displayed in a List "[[]]", remove the outer bracket.
            coordinates = coordinates[0]
        
        geometry = None
        if geo_type == GeoTypes.POINT:            
            geometry = Point(coordinates)
        elif geo_type == GeoTypes.POLYGON:    
            geometry = Polygon(coordinates)
        elif geo_type == GeoTypes.LINE_STRING:
            geometry = LineString(coordinates)
        else:
            raise Exception("Unexpected geo_type: {}".format(geo_type))

        feature_json = Feature(geometry=geometry, properties=feature["properties"])

        return feature_json, geo_type

    def map_coordinates_recursive(self, coordinates, func):        
        """
        Recursively traverses the array of coordinates (depth first) and applies the specified function
        """
        tmp = []
        for coord in coordinates:          
            is_coordinate_tuple = len(coord) == 2 and all(isinstance(c, int) for c in coord)
            if is_coordinate_tuple:
                newval = func(coord)
                tmp.append(newval)
            else:
                tmp.append(self.map_coordinates_recursive(coord, func))
        return tmp

    @staticmethod
    def _calculate_geometry(self, coordinates, tile):
        """
        Does a mercator transformation on the specified coordinate tuple
        """
        # calculate the mercator geometry using external library
        # geometry:: 0: zoom, 1: easting, 2: northing
        tmp = GlobalMercator().TileBounds(tile.column, tile.row, tile.zoom_level)
        delta_x = tmp[2] - tmp[0]
        delta_y = tmp[3] - tmp[1]
        merc_easting = int(tmp[0] + delta_x / self._extent * coordinates[0])
        merc_northing = int(tmp[1] + delta_y / self._extent * coordinates[1])
        return [merc_easting, merc_northing]

    def create_unique_file_name(self, ending = "geojson"):
        unique_name = "{}.{}".format(uuid.uuid4(), ending)
        return os.path.join(self.temp_dir, unique_name)