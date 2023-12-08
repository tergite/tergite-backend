from os import environ

TEST_POSTPROC_PLOTTING = "False"
TEST_DEFAULT_PREFIX = "test"
TEST_STORAGE_ROOT = "/tmp/jobs"

TEST_LOGFILE_DOWNLOAD_POOL_DIRNAME = "logfile_download_pool"
TEST_LOGFILE_UPLOAD_POOL_DIRNAME = "logfile_upload_pool"
TEST_JOB_UPLOAD_POOL_DIRNAME = "job_upload_pool"
TEST_JOB_EXECUTION_POOL_DIRNAME = "job_execution_pool"
TEST_JOB_PRE_PROC_POOL_DIRNAME = "job_pre_proc_pool"
TEST_STORAGE_PREFIX_DIRNAME = TEST_DEFAULT_PREFIX
TEST_JOB_SUPERVISOR_LOG = "job_supervisor.log"

TEST_LABBER_MACHINE_ROOT_URL = "http://localhost:8005"
TEST_QUANTIFY_MACHINE_ROOT_URL = "http://localhost:8004"
TEST_MSS_MACHINE_ROOT_URL = "http://localhost:8002"
TEST_BCC_MACHINE_ROOT_URL = "http://localhost:8000"
TEST_BCC_PORT = 8000
TEST_DB_MACHINE_ROOT_URL = "mongodb://localhost:27017"
TEST_CALIBRATION_SUPERVISOR_PORT = 8003

TEST_MSS_APP_TOKEN = "some-mss-app-token-for-testing"


def setup_test_env():
    """Sets up the test environment.

    It should be run before any imports
    """
    environ["APP_SETTINGS"] = "test"
    environ["POSTPROC_PLOTTING"] = TEST_POSTPROC_PLOTTING
    environ["DEFAULT_PREFIX"] = TEST_DEFAULT_PREFIX
    environ["STORAGE_ROOT"] = TEST_STORAGE_ROOT

    environ["LOGFILE_DOWNLOAD_POOL_DIRNAME"] = TEST_LOGFILE_DOWNLOAD_POOL_DIRNAME
    environ["LOGFILE_UPLOAD_POOL_DIRNAME"] = TEST_LOGFILE_UPLOAD_POOL_DIRNAME
    environ["JOB_UPLOAD_POOL_DIRNAME"] = TEST_JOB_UPLOAD_POOL_DIRNAME
    environ["JOB_EXECUTION_POOL_DIRNAME"] = TEST_JOB_EXECUTION_POOL_DIRNAME
    environ["JOB_PRE_PROC_POOL_DIRNAME"] = TEST_JOB_PRE_PROC_POOL_DIRNAME
    environ["STORAGE_PREFIX_DIRNAME"] = TEST_STORAGE_PREFIX_DIRNAME
    environ["JOB_SUPERVISOR_LOG"] = TEST_JOB_SUPERVISOR_LOG

    environ["LABBER_MACHINE_ROOT_URL"] = TEST_LABBER_MACHINE_ROOT_URL
    environ["QUANTIFY_MACHINE_ROOT_URL"] = TEST_QUANTIFY_MACHINE_ROOT_URL
    environ["MSS_MACHINE_ROOT_URL"] = TEST_MSS_MACHINE_ROOT_URL
    environ["BCC_MACHINE_ROOT_URL"] = TEST_BCC_MACHINE_ROOT_URL
    environ["BCC_PORT"] = f"{TEST_BCC_PORT}"
    environ["DB_MACHINE_ROOT_URL"] = TEST_DB_MACHINE_ROOT_URL
    environ["CALIBRATION_SUPERVISOR_PORT"] = f"{TEST_CALIBRATION_SUPERVISOR_PORT}"

    environ["FETCH_DISCRIMINATOR"] = f"True"
    environ["MSS_APP_TOKEN"] = TEST_MSS_APP_TOKEN
