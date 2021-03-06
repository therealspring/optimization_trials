"""Minimize land area selction while maximizing service."""
import glob
import hashlib
import logging
import multiprocessing
import os
import subprocess
import sys
import tempfile

from osgeo import gdal
from osgeo import ogr
from osgeo import osr
import numpy
import pygeoprocessing
import pygeoprocessing.routing
import taskgraph

gdal.SetCacheMax(2**27)

BASE_DATA_DIR = 'data'
WORKSPACE_DIR = 'workspace_dir'
CHURN_DIR = os.path.join(WORKSPACE_DIR, 'churn')

# each bucket has a handful of .tifs and a .gpkg, the fieldnam is the fielname
# to iterate through
BUCKET_FIELDNAME_LIST = [
    # ('gs://critical-natural-capital-ecoshards/realized_service_ecoshards/'
    #  'by_country', 'iso3'),
    # ('gs://critical-natural-capital-ecoshards/realized_service_ecoshards/'
    #  'by_eez', 'ISO_SOV1'),
    ('gs://critical-natural-capital-ecoshards/realized_service_ecoshards/'
     'by_country', 'iso3',
     'gs://critical-natural-capital-ecoshards/realized_service_ecoshards/'
     'by_country/'
     'countries_singlepart_md5_b7aaa5bc55cefc1f28d9655629c2c702.gpkg'),
    ]

ISO_CODES_TO_SKIP = ['ATA']

TARGET_NODATA = -1
PROP_NODATA = -1
logging.basicConfig(
    filename='log.txt',
    level=logging.DEBUG,
    format=(
        '%(asctime)s (%(relativeCreated)d) %(levelname)s %(name)s'
        ' [%(funcName)s:%(lineno)d] %(message)s'))
LOGGER = logging.getLogger(__name__)
logging.getLogger('taskgraph').setLevel(logging.INFO)


def sum_raster(raster_path_band):
    """Sum the raster and return the result."""
    nodata = pygeoprocessing.get_raster_info(
        raster_path_band[0])['nodata'][raster_path_band[1]-1]

    raster_sum = 0.0
    for _, array in pygeoprocessing.iterblocks(raster_path_band):
        valid_mask = ~numpy.isclose(array, nodata)
        raster_sum += numpy.sum(array[valid_mask])

    return raster_sum


def make_neighborhood_hat_kernel(kernel_size, kernel_filepath):
    """Make a hat kernel that's the sum in the center and 1 <= kernel_size.

    Args:
        kernel_size (int): kernel should be kernel_size X kernel_size
        kernel_filepath (str): path to target kernel.

    Returns:
        None

    """
    driver = gdal.GetDriverByName('GTiff')
    kernel_raster = driver.Create(
        kernel_filepath, kernel_size, kernel_size, 1, gdal.GDT_Float32)
    kernel_raster.SetGeoTransform([0, 1, 0, 0, 0, -1])
    srs = osr.SpatialReference()
    srs.SetWellKnownGeogCS('WGS84')
    kernel_raster.SetProjection(srs.ExportToWkt())

    kernel_band = kernel_raster.GetRasterBand(1)
    kernel_band.SetNoDataValue(-9999)

    kernel_array = (numpy.sqrt(numpy.sum(
        [(index - kernel_size//2)**2
         for index in numpy.meshgrid(
            range(kernel_size), range(kernel_size))], axis=0)) <=
        kernel_size//2).astype(numpy.uint8)

    # make the center the sum of the area of the circle so it's always on
    kernel_array[kernel_array//2, kernel_array//2] = (
        3.14159 * (kernel_size//2+1)**2)
    kernel_band.WriteArray(kernel_array)
    kernel_band = None
    kernel_raster = None


def smooth_mask(base_mask_path, smooth_radius, target_smooth_mask_path):
    """Fill in gaps in base mask if there are neighbors.

    Args:
        base_mask_path (str): path to base raster, should be 0, 1 and nodata.
        smooth_radius (int): how far to smooth out at a max radius?
        target_smooth_mask_path (str): target smoothed file.

    Returns:
        None.

    """
    kernel_size = smooth_radius*2+1
    working_dir = tempfile.mkdtemp(
        dir=os.path.dirname(target_smooth_mask_path))
    kernel_path = os.path.join(working_dir, f'kernel_{kernel_size}.tif')
    make_neighborhood_hat_kernel(kernel_size, kernel_path)

    convolved_raster_path = os.path.join(working_dir, 'convolved_mask.tif')
    byte_nodata = 255
    pygeoprocessing.convolve_2d(
        (base_mask_path, 1), (kernel_path, 1), convolved_raster_path,
        ignore_nodata=False, working_dir=working_dir, mask_nodata=False,
        target_nodata=TARGET_NODATA)

    # set required proportion of coverage to turn on a pixel, lets make it a
    # quarter wedge.
    proportion_covered = 0.01
    threshold_val = proportion_covered * 3.14159 * (smooth_radius+1)**2
    pygeoprocessing.raster_calculator(
        [(convolved_raster_path, 1), (threshold_val, 'raw'),
         (TARGET_NODATA, 'raw'), (byte_nodata, 'raw'), ], threshold_op,
        target_smooth_mask_path, gdal.GDT_Byte, byte_nodata)

    # try:
    #     shutil.rmtree(working_dir)
    # except OSError:
    #     LOGGER.warn("couldn't delete %s", working_dir)
    #     pass


def threshold_op(base_array, threshold_val, base_nodata, target_nodata):
    """Threshold base to 1 where val >= threshold_val."""
    result = numpy.empty(base_array.shape, dtype=numpy.uint8)
    result[:] = target_nodata
    valid_mask = ~numpy.isclose(base_array, base_nodata) & (
        ~numpy.isclose(base_array, 0))
    result[valid_mask] = base_array[valid_mask] >= threshold_val
    return result


def proportion_op(base_array, total_sum, base_nodata, target_nodata):
    """Divide base by total and guard against nodata."""
    result = numpy.empty(base_array.shape, dtype=numpy.float64)
    result[:] = target_nodata
    valid_mask = ~numpy.isclose(base_array, base_nodata)
    result[valid_mask] = (
        base_array[valid_mask].astype(numpy.float64) / total_sum)
    return result


def sum_rasters_op(nodata, *array_list):
    """Sum all non-nodata pixels in array_list."""
    result = numpy.zeros(array_list[0].shape)
    total_valid_mask = numpy.zeros(array_list[0].shape, dtype=numpy.bool)
    for array in array_list:
        valid_mask = ~numpy.isclose(array, nodata)
        total_valid_mask |= valid_mask
        result[valid_mask] += array[valid_mask]
    result[~total_valid_mask] = nodata
    return result


def copy_gs(gs_uri, target_dir, token_file_path):
    """Copy uri dir to target and touch a token_file."""
    LOGGER.debug(' to copy %s to %s', gs_uri, target_dir)
    try:
        os.makedirs(target_dir)
    except OSError:
        pass
    gsutil_path = '/usr/local/gcloud-sdk/google-cloud-sdk/bin/gsutil'
    if not os.path.exists(gsutil_path):
        gsutil_path = 'gsutil'

    subprocess.run(
        f'{gsutil_path} cp -r "{gs_uri}/*" "{target_dir}"',
        shell=True, check=True)
    with open(token_file_path, 'w') as token_file:
        token_file.write("done")


def copy_gs_single(gs_uri, target_path):
    """Copy uri dir to target and touch a token_file."""
    LOGGER.debug(' to copy %s to %s', gs_uri, target_path)
    try:
        os.makedirs(os.path.dirname(target_path))
    except OSError:
        pass
    gsutil_path = '/usr/local/gcloud-sdk/google-cloud-sdk/bin/gsutil'
    if not os.path.exists(gsutil_path):
        gsutil_path = 'gsutil'

    subprocess.run(
        f'{gsutil_path} cp "{gs_uri}" "{target_path}"',
        shell=True, check=True)


def extract_feature(
        base_vector_path, base_fieldname, base_fieldname_value,
        target_vector_path):
    """Extract a feature from base into a new vector.

    Args:
        base_vector_path (str): path to a multipolygon vector
        base_fieldname (str): name of fieldname to filter on
        base_fieldname_value (str): value of fieldname to filter on
        target_vector_path (str): path to target vector.

    Returns:
        None.

    """
    try:
        os.makedirs(os.path.dirname(target_vector_path))
    except OSError:
        pass
    vector = gdal.OpenEx(base_vector_path, gdal.OF_VECTOR)
    layer = vector.GetLayer()
    layer.SetAttributeFilter(f"{base_fieldname}='{base_fieldname_value}'")
    country_feature = next(iter(layer))
    country_geometry = country_feature.GetGeometryRef()
    gpkg_driver = ogr.GetDriverByName('GPKG')

    local_country_vector = gpkg_driver.CreateDataSource(target_vector_path)
    # create the layer
    local_layer = local_country_vector.CreateLayer(
        os.path.basename(os.path.splitext(base_fieldname_value)[0]),
        layer.GetSpatialRef(), ogr.wkbMultiPolygon)
    layer_defn = local_layer.GetLayerDefn()
    new_feature = ogr.Feature(layer_defn)
    new_feature.SetGeometry(country_geometry.Clone())
    country_geometry = None
    local_layer.CreateFeature(new_feature)
    new_feature = None
    local_layer = None
    local_country_vector = None


def main():
    """Entry point."""
    # convert raster list to just 1-10 integer
    for dir_path in [WORKSPACE_DIR, CHURN_DIR]:
        try:
            os.makedirs(dir_path)
        except OSError:
            pass

    task_graph = taskgraph.TaskGraph(WORKSPACE_DIR, -1)

    for bucket_uri, fieldname, bucket_vector_uri in BUCKET_FIELDNAME_LIST:
        m = hashlib.md5()
        m.update(bucket_uri.encode('utf-8'))
        local_churn_dir = os.path.join(CHURN_DIR, m.hexdigest())
        local_download_dir = os.path.join(local_churn_dir, 'downloads')
        token_file = os.path.join(
            local_download_dir, f'{os.path.basename(bucket_uri)}.token')
        copy_gs_dir_task = task_graph.add_task(
            func=copy_gs,
            args=(bucket_uri, local_download_dir, token_file),
            target_path_list=[token_file],
            task_name=f'copy {bucket_uri}')

        global_vector_path = os.path.join(
            local_download_dir, os.path.basename(bucket_vector_uri))
        copy_gs_task = task_graph.add_task(
            func=copy_gs_single,
            args=(
                bucket_vector_uri, global_vector_path),
            target_path_list=[global_vector_path],
            task_name=f'copy {bucket_uri}')

        copy_gs_dir_task.join()
        copy_gs_task.join()

        # we know there's a .gpkg in there

        base_raster_path_list = [
            path for path in glob.glob(os.path.join(
                local_download_dir, '*.tif'))]
        LOGGER.debug(base_raster_path_list)
        clipped_pixel_length = min([
            pygeoprocessing.get_raster_info(path)['pixel_size'][0]
            for path in base_raster_path_list])

        # get fieldname set
        global_vector = gdal.OpenEx(global_vector_path, gdal.OF_VECTOR)
        global_layer = global_vector.GetLayer()
        field_list = [
            feature.GetField(fieldname) for feature in global_layer]
        fid_order_list = []
        for feature in global_layer:
            geom = feature.GetGeometryRef()
            fid_order_list.append(
                (feature.GetFID(), feature.GetField(fieldname), geom.Area()))
            LOGGER.debug(fid_order_list[-1])
            geom = None
            feature = None

        LOGGER.debug('sort list')
        fid_order_list = sorted(fid_order_list, key=lambda x: x[-1])
        global_layer = None
        global_vector = None

        LOGGER.debug('start pool')
        # do india first
        worker_pool = multiprocessing.Pool(
            multiprocessing.cpu_count(), maxtasksperchild=1)
        LOGGER.debug('process this list: %s', field_list)
        for fid, field_val, _ in fid_order_list:
            if field_val in ISO_CODES_TO_SKIP:
                continue

            local_working_dir = os.path.join(
                local_churn_dir, os.path.basename(bucket_uri), field_val)
            try:
                os.makedirs(local_working_dir)
            except OSError:
                pass

            LOGGER.debug('%s: %s', os.path.basename(bucket_uri), field_val)

            local_country_vector_path = os.path.join(
                local_working_dir, f'{field_val}.gpkg')
            extract_task = task_graph.add_task(
                func=extract_feature,
                args=(global_vector_path, fieldname, field_val,
                      local_country_vector_path),
                hash_target_files=False,
                target_path_list=[local_country_vector_path],
                task_name=f'extract {field_val}')

            clipped_raster_path_list = [
                os.path.join(
                    local_working_dir, 'clipped',
                    os.path.basename(raster_path))
                for raster_path in base_raster_path_list]

            align_task = task_graph.add_task(
                func=pygeoprocessing.align_and_resize_raster_stack,
                args=(
                    base_raster_path_list, clipped_raster_path_list,
                    ['near'] * len(clipped_raster_path_list),
                    [clipped_pixel_length, -clipped_pixel_length],
                    'intersection',  # for debugging: [80, 20, 81, 21], # area in mid india
                    ),
                kwargs={
                    'base_vector_path_list': [local_country_vector_path],
                    'vector_mask_options': {
                        'mask_vector_path': local_country_vector_path,
                    }
                },
                dependent_task_list=[extract_task],
                ignore_path_list=[local_country_vector_path],
                target_path_list=clipped_raster_path_list,
                task_name=f'clip align task for {field_val}')
            align_task.join()

            target_suffix = field_val
            logging.getLogger('pygeoprocessing').setLevel(logging.DEBUG)
            local_output_dir = os.path.join(
                local_churn_dir, 'output', field_val)

            expected_target_csv_path = os.path.join(
                local_output_dir, f'results_{target_suffix}.csv')

            if os.path.exists(expected_target_csv_path):
                LOGGER.debug('%s exists, so skip', expected_target_csv_path)
            else:
                worker_pool.apply_async(
                    func=pygeoprocessing.raster_optimization,
                    args=(
                        [(x, 1) for x in clipped_raster_path_list],
                        local_working_dir, local_output_dir),
                    kwds={'target_suffix': target_suffix},
                    error_callback=optimization_error_handler)

    task_graph.join()
    task_graph.close()
    worker_pool.close()
    worker_pool.join()
    LOGGER.debug('task graph is all done')
    task_graph._terminate()


def optimization_error_handler(e):
    """Exception handler for worker pool."""
    with open('error.txt', 'a') as error_file:
        error_string = 'exception occurred: %e', str(e)
        LOGGER.error(error_string)
        error_file.write(error_string + '\n')


if __name__ == '__main__':
    main()
