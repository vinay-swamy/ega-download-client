#!/usr/bin/env python3

import argparse
import concurrent.futures
import hashlib
import json
import logging
import logging.handlers
import math
import os
import platform
import random
import sys
import time

import htsget
import psutil
import requests
from tqdm import tqdm

from pyega3.auth_client import AuthClient
from pyega3.data_client import DataClient
from pyega3.credentials import Credentials
from pyega3.data_file import DataFile
from pyega3.server_config import ServerConfig

version = "3.4.1"
session_id = random.getrandbits(32)
logging_level = logging.INFO


TEMPORARY_FILES_SHOULD_BE_DELETED = False

LEGACY_DATASETS = ["EGAD00000000003", "EGAD00000000004", "EGAD00000000005", "EGAD00000000006", "EGAD00000000007",
                   "EGAD00000000008", "EGAD00000000009", "EGAD00000000025", "EGAD00000000029", "EGAD00000000043",
                   "EGAD00000000048", "EGAD00000000049", "EGAD00000000051", "EGAD00000000052", "EGAD00000000053",
                   "EGAD00000000054", "EGAD00000000055", "EGAD00000000056", "EGAD00000000057", "EGAD00000000060",
                   "EGAD00000000114", "EGAD00000000119", "EGAD00000000120", "EGAD00000000121", "EGAD00000000122",
                   "EGAD00001000132", "EGAD00010000124", "EGAD00010000144", "EGAD00010000148", "EGAD00010000150",
                   "EGAD00010000158", "EGAD00010000160", "EGAD00010000162", "EGAD00010000164", "EGAD00010000246",
                   "EGAD00010000248", "EGAD00010000250", "EGAD00010000256", "EGAD00010000444"]


def get_client_ip():
    endpoint = 'https://ipinfo.io/json'
    unknown_status = 'Unknown'
    try:
        response = requests.get(endpoint, verify=True)
        if response.status_code != 200:
            print('Status:', response.status_code, 'Problem with the request.')
            return unknown_status

        data = response.json()
        return data['ip']
    except Exception:
        logging.error("Failed to obtain IP address")
        return unknown_status


CLIENT_IP = get_client_ip()


def get_standart_headers():
    return {'Client-Version': version, 'Session-Id': str(session_id), 'client-ip': CLIENT_IP}


def api_list_authorized_datasets(data_client):
    """List datasets to which the credentialed user has authorized access"""

    reply = data_client.get_json("/metadata/datasets")

    if reply is None:
        logging.error(
            "You do not currently have access to any datasets at EGA according to our databases."
            " If you believe you should have access please contact helpdesk on helpdesk@ega-archive.org")
        sys.exit()

    return reply


def pretty_print_authorized_datasets(reply):
    logging.info("Dataset ID")
    logging.info("-----------------")
    for datasetid in reply:
        logging.info(datasetid)


def api_list_files_in_dataset(data_client, dataset):
    if dataset in LEGACY_DATASETS:
        logging.error(f"This is a legacy dataset {dataset}. Please contact the EGA helpdesk for more information.")
        sys.exit()

    if dataset not in api_list_authorized_datasets(data_client):
        logging.error(f"Dataset '{dataset}' is not in the list of your authorized datasets.")
        sys.exit()

    reply = data_client.get_json(f"/metadata/datasets/{dataset}/files")

    if reply is None:
        logging.error(f"List files in dataset {dataset} failed")
        sys.exit()

    return reply


def status_ok(status_string):
    if status_string == "available":
        return True
    else:
        return False


def pretty_print_files_in_dataset(reply):
    """
    Print a table of files in authorized dataset from api call api_list_files_in_dataset

        {
           "checksumType": "MD5",
            "unencryptedChecksum": "MD5SUM678901234567890123456789012",
            "fileName": "EGAZ00001314035/b37/NA12878.bam.bai.cip",
            "displayFileName": "NA12878.bam.bai.cip",
            "fileStatus": "available",
            "fileSize": 8949984,
            "datasetId": "EGAD00001003338",
            "fileId": "EGAF00001753747"
        }

    """
    format_string = "{:15} {:6} {:12} {:36} {}"

    logging.info(format_string.format("File ID", "Status", "Bytes", "Check sum", "File name"))
    for res in reply:
        logging.info(format_string.format(res['fileId'], status_ok(res['fileStatus']), str(res['fileSize']),
                                          res['unencryptedChecksum'], res['displayFileName']))

    logging.info('-' * 80)
    logging.info("Total dataset size = %.2f GB " % (sum(r['fileSize'] for r in reply) / (1024 * 1024 * 1024.0)))


def print_local_file_info_genomic_range(prefix_str, file, gr_args):
    logging.info(
        f"{prefix_str}'{os.path.abspath(file)}'({os.path.getsize(file)} bytes, referenceName={gr_args[0]},"
        f" referenceMD5={gr_args[1]}, start={gr_args[2]}, end={gr_args[3]}, format={gr_args[4]})"
    )


def is_genomic_range(genomic_range_args):
    if not genomic_range_args:
        return False
    return genomic_range_args[0] is not None or genomic_range_args[1] is not None


def generate_output_filename(folder, file_id, file_name, genomic_range_args):
    ext_to_remove = ".cip"
    if file_name.endswith(ext_to_remove):
        file_name = file_name[:-len(ext_to_remove)]
    name, ext = os.path.splitext(os.path.basename(file_name))

    genomic_range = ''
    if is_genomic_range(genomic_range_args):
        genomic_range = "_genomic_range_" + (genomic_range_args[0] or genomic_range_args[1])
        genomic_range += '_' + (str(genomic_range_args[2]) or '0')
        genomic_range += '_' + (str(genomic_range_args[3]) or '')
        format_ext = '.' + (genomic_range_args[4] or '').strip().lower()
        if format_ext != ext and len(format_ext) > 1:
            ext += format_ext

    ret_val = os.path.join(folder, file_id, name + genomic_range + ext)
    logging.debug(f"Output file:'{ret_val}'")
    return ret_val


def download_file_retry(
        data_client, file_id, num_connections, output_file,
        genomic_range_args,
        max_retries, retry_wait, display_file_name, file_name, file_size, check_sum, key=None):
    if file_name.endswith(".gpg"):
        logging.info(
            "GPG files are not supported, please use the Java client"
            " - https://ega-archive.org/download/using-ega-download-client")
        return

    file = DataFile(data_client, file_id, size=file_size, unencrypted_checksum=check_sum)

    logging.info(f"File Id: '{file_id}'({file_size} bytes).")

    if output_file is None:
        output_file = generate_output_filename(os.getcwd(), file_id, display_file_name, genomic_range_args)
    dir = os.path.dirname(output_file)
    if not os.path.exists(dir) and len(dir) > 0:
        os.makedirs(dir)

    hdd = psutil.disk_usage(os.getcwd())
    logging.info(f"Total space : {hdd.total / (2 ** 30):.2f} GiB")
    logging.info(f"Used space : {hdd.used / (2 ** 30):.2f} GiB")
    logging.info(f"Free space : {hdd.free / (2 ** 30):.2f} GiB")

    if is_genomic_range(genomic_range_args):
        with open(output_file, 'wb') as output:
            htsget.get(
                f"{data_client.htsget_url}/files/{file_id}",
                output,
                reference_name=genomic_range_args[0], reference_md5=genomic_range_args[1],
                start=genomic_range_args[2], end=genomic_range_args[3],
                data_format=genomic_range_args[4],
                max_retries=sys.maxsize if max_retries < 0 else max_retries,
                retry_wait=retry_wait,
                bearer_token=data_client.auth_client.token)
        print_local_file_info_genomic_range('Saved to : ', output_file, genomic_range_args)
        return

    done = False
    num_retries = 0
    while not done:
        try:
            file.download_file(num_connections, key, output_file)
            done = True
        except Exception as e:
            logging.exception(e)
            if num_retries == max_retries:
                if TEMPORARY_FILES_SHOULD_BE_DELETED:
                    delete_temporary_files(DataFile.TEMPORARY_FILES)

                raise e
            time.sleep(retry_wait)
            num_retries += 1
            logging.info(f"retry attempt {num_retries}")


def download_dataset(
        data_client, dataset_id, num_connections, output_dir, genomic_range_args, max_retries=5,
        retry_wait=5, key=None):
    if dataset_id in LEGACY_DATASETS:
        logging.error(f"This is a legacy dataset {dataset_id}. Please contact the EGA helpdesk for more information.")
        sys.exit()

    if dataset_id not in api_list_authorized_datasets(data_client):
        logging.info(f"Dataset '{dataset_id}' is not in the list of your authorized datasets.")
        return

    reply = api_list_files_in_dataset(data_client, dataset_id)
    for res in reply:
        try:
            if status_ok(res['fileStatus']):
                output_file = None if (output_dir is None) else generate_output_filename(output_dir, res['fileId'],
                                                                                         res['displayFileName'],
                                                                                         genomic_range_args)
                download_file_retry(data_client, res['fileId'], num_connections, output_file, genomic_range_args,
                                    max_retries, retry_wait, res['displayFileName'], res['fileName'], res['fileSize'],
                                    res['unencryptedChecksum'], key)
        except Exception as e:
            logging.exception(e)


def print_debug_info(url, reply_json, *args):
    logging.debug(f"Request URL : {url}")
    if reply_json is not None:
        logging.debug("Response    :\n %.1200s" % json.dumps(reply_json, indent=4))

    for a in args:
        logging.debug(a)


def delete_temporary_files(temporary_files):
    try:
        for temporary_file in temporary_files:
            logging.debug(f'Deleting the {temporary_file} temporary file...')
            os.remove(temporary_file)
    except FileNotFoundError as ex:
        logging.error(f'Could not delete the temporary file: {ex}')


def main():
    parser = argparse.ArgumentParser(description="Download from EMBL EBI's EGA (European Genome-phenome Archive)")
    parser.add_argument("-d", "--debug", action="store_true", help="Extra debugging messages")
    parser.add_argument("-cf", "--config-file", dest='config_file',
                        help='JSON file containing credentials/config e.g.{"username":"user1","password":"toor"}')
    parser.add_argument("-sf", "--server-file", dest='server_file',
                        help='JSON file containing server config e.g.{"url_auth":"aai url","url_api":"api url", "url_api_ticket":"htsget url", "client_secret":"client secret"}')
    parser.add_argument("-c", "--connections", type=int, default=1,
                        help="Download using specified number of connections")
    parser.add_argument("-t", "--test", action="store_true", help="Test user activated")

    subparsers = parser.add_subparsers(dest="subcommand", help="subcommands")

    parser_ds = subparsers.add_parser("datasets", help="List authorized datasets")

    parser_dsinfo = subparsers.add_parser("files", help="List files in a specified dataset")
    parser_dsinfo.add_argument("identifier", help="Dataset ID (e.g. EGAD00000000001)")

    parser_fetch = subparsers.add_parser("fetch", help="Fetch a dataset or file")
    parser_fetch.add_argument("identifier", help="Id for dataset (e.g. EGAD00000000001) or file (e.g. EGAF12345678901)")

    parser_fetch.add_argument(
        "--reference-name", "-r", type=str, default=None,
        help=(
            "The reference sequence name, for example 'chr1', '1', or 'chrX'. "
            "If unspecified, all data is returned."))
    parser_fetch.add_argument(
        "--reference-md5", "-m", type=str, default=None,
        help=(
            "The MD5 checksum uniquely representing the requested reference "
            "sequence as a lower-case hexadecimal string, calculated as the MD5 "
            "of the upper-case sequence excluding all whitespace characters."))
    parser_fetch.add_argument(
        "--start", "-s", type=int, default=None,
        help=(
            "The start position of the range on the reference, 0-based, inclusive. "
            "If specified, reference-name or reference-md5 must also be specified."))
    parser_fetch.add_argument(
        "--end", "-e", type=int, default=None,
        help=(
            "The end position of the range on the reference, 0-based exclusive. If "
            "specified, reference-name or reference-md5 must also be specified."))
    parser_fetch.add_argument(
        "--format", "-f", type=str, default=None, choices=["BAM", "CRAM"], help="The format of data to request.")

    parser_fetch.add_argument(
        "--max-retries", "-M", type=int, default=5,
        help="The maximum number of times to retry a failed transfer. Any negative number means infinite number of retries.")

    parser_fetch.add_argument(
        "--retry-wait", "-W", type=float, default=60,
        help="The number of seconds to wait before retrying a failed transfer.")

    parser_fetch.add_argument("--saveto", nargs='?', help="Output file(for files)/output dir(for datasets)")

    parser_fetch.add_argument("--delete-temp-files", action="store_true",
                              help="Do not keep those temporary, partial files "
                                   "which were left on the disk after a failed transfer.")

    args = parser.parse_args()
    if args.debug:
        global logging_level
        logging_level = logging.DEBUG

    logging.basicConfig(level=logging_level,
                        format='%(asctime)s %(message)s',
                        datefmt='[%Y-%m-%d %H:%M:%S %z]',
                        handlers=[
                            logging.handlers.RotatingFileHandler("pyega3_output.log",
                                                                 maxBytes=5 * 1024 * 1024,
                                                                 backupCount=1),
                            logging.StreamHandler()
                        ])

    logging.info("")
    logging.info(f"pyEGA3 - EGA python client version {version} (https://github.com/EGA-archive/ega-download-client)")
    logging.info("Parts of this software are derived from pyEGA (https://github.com/blachlylab/pyega) by James Blachly")
    logging.info(f"Python version : {platform.python_version()}")
    logging.info(f"OS version : {platform.system()} {platform.version()}")

    root_dir = os.path.split(os.path.realpath(__file__))[0]
    config_file_path = os.path.join(root_dir, "config", "default_credential_file.json")

    if args.test:
        credentials = Credentials.from_file(config_file_path)
    elif args.config_file is None:
        credentials = Credentials.from_file("credential_file.json")
    else:
        credentials = Credentials.from_file(args.config_file)

    if args.server_file is not None:
        server_config = ServerConfig.from_file(args.server_file)
    else:
        server_config = ServerConfig.from_file(ServerConfig.default_config_path())

    logging.info(f"Server URL: {server_config.url_api}")
    logging.info(f"Session-Id: {session_id}")

    auth_client = AuthClient(server_config.url_auth, server_config.client_secret, get_standart_headers())
    auth_client.credentials = credentials

    data_client = DataClient(server_config.url_api, server_config.url_api_ticket, auth_client, get_standart_headers())

    if args.subcommand == "datasets":
        reply = api_list_authorized_datasets(data_client)
        pretty_print_authorized_datasets(reply)

    if args.subcommand == "files":
        if args.identifier[3] != 'D':
            logging.error("Unrecognized identifier - please use EGAD accession for dataset requests")
            sys.exit()
        token = auth_client.token
        reply = api_list_files_in_dataset(token, args.identifier)
        pretty_print_files_in_dataset(reply)

    elif args.subcommand == "fetch":
        genomic_range_args = (args.reference_name, args.reference_md5, args.start, args.end, args.format)

        if args.delete_temp_files:
            global TEMPORARY_FILES_SHOULD_BE_DELETED
            TEMPORARY_FILES_SHOULD_BE_DELETED = True

        if args.identifier[3] == 'D':
            download_dataset(data_client, args.identifier, args.connections, args.saveto,
                             genomic_range_args,
                             args.max_retries, args.retry_wait, credentials.key)
        elif args.identifier[3] == 'F':
            file = DataFile(data_client, args.identifier)
            download_file_retry(data_client, args.identifier, args.connections, args.saveto, genomic_range_args,
                                args.retry_wait, file.display_name, file.name, file.size, file.unencrypted_checksum,
                                args.max_retries, credentials.key)
        else:
            logging.error(
                "Unrecognized identifier - please use EGAD accession for dataset request"
                " or EGAF accession for individual file requests")
            sys.exit()

        logging.info("Download complete")


if __name__ == "__main__":
    main()
