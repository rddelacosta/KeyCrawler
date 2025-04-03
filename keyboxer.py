import requests
import hashlib
import os
import zipfile
import gzip
import io
import re
import tarfile

from lxml import etree
from pathlib import Path
from dotenv import load_dotenv

from check import keybox_check as CheckValid

session = requests.Session()

# Load environment variables from .env file
load_dotenv()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

if not GITHUB_TOKEN:
    raise ValueError("GITHUB_TOKEN is not set in the .env file")

# Search query
search_query = "<AndroidAttestation>"
search_url = f"https://api.github.com/search/code?q={search_query}"

# Headers for the API request
headers = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}

save = Path(__file__).resolve().parent / "keys"
cache_file = Path(__file__).resolve().parent / "cache.txt"
cached_urls = set(open(cache_file, "r").readlines())


# Function to check if content is a valid archive format
def is_archive(content):
    # Try to identify ZIP files
    if content.startswith(b'PK\x03\x04'):
        return 'zip'
    # Try to identify GZIP files
    elif content.startswith(b'\x1f\x8b'):
        return 'gzip'
    # Try to identify TAR files (less reliable header check)
    elif content[257:257+5] == b'ustar':
        return 'tar'
    return None


# Function to extract XML files from an archive
def extract_xml_from_archive(content, archive_type):
    xml_files = []
    
    try:
        if archive_type == 'zip':
            with io.BytesIO(content) as content_io:
                with zipfile.ZipFile(content_io) as zip_ref:
                    # List all files in the ZIP
                    file_list = zip_ref.namelist()
                    
                    # Filter for XML files
                    for file_name in file_list:
                        if file_name.lower().endswith('.xml'):
                            with zip_ref.open(file_name) as xml_file:
                                xml_content = xml_file.read()
                                xml_files.append(xml_content)
        
        elif archive_type == 'gzip':
            with io.BytesIO(content) as content_io:
                with gzip.GzipFile(fileobj=content_io) as gz_file:
                    extracted_content = gz_file.read()
                    # Check if the extracted content is XML
                    if extracted_content.startswith(b'<?xml') or b'<AndroidAttestation>' in extracted_content:
                        xml_files.append(extracted_content)
        
        elif archive_type == 'tar':
            with io.BytesIO(content) as content_io:
                with tarfile.open(fileobj=content_io, mode='r') as tar_ref:
                    for member in tar_ref.getmembers():
                        if member.name.lower().endswith('.xml'):
                            f = tar_ref.extractfile(member)
                            if f:
                                xml_content = f.read()
                                xml_files.append(xml_content)
    
    except (zipfile.BadZipFile, gzip.BadGzipFile, tarfile.ReadError, Exception) as e:
        print(f"Error extracting from archive: {e}")
    
    return xml_files


# Function to process XML content
def process_xml_content(url, content):
    try:
        root = etree.fromstring(content)
        # Get the canonical form (C14N)
        canonical_xml = etree.tostring(root, method="c14n")
        # Hash the canonical XML
        hash_value = hashlib.sha256(canonical_xml).hexdigest()
        file_name_save = save / (hash_value + ".xml")
        if not file_name_save.exists() and CheckValid(content):
            print(f"{url} is new")
            with open(file_name_save, "wb") as f:
                f.write(content)
            return True
    except etree.XMLSyntaxError:
        pass
    return False


# Function to fetch and process search results
def fetch_and_process_results(page):
    params = {"per_page": 100, "page": page}
    response = session.get(search_url, headers=headers, params=params)
    if response.status_code != 200:
        raise RuntimeError(f"Failed to retrieve search results: {response.status_code}")
    search_results = response.json()
    if "items" in search_results:
        for item in search_results["items"]:
            file_name = item["name"]
            raw_url: str = (
                item["html_url"].replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
            )
            # check if the file exists in cache
            if raw_url + "\n" in cached_urls:
                continue
            else:
                cached_urls.add(raw_url + "\n")
            # Fetch the file content
            file_content = fetch_file_content(raw_url)
            if not file_content:
                continue
            
            # Check if the content is an archive
            archive_type = is_archive(file_content)
            if archive_type:
                # Extract XML files from the archive
                xml_files = extract_xml_from_archive(file_content, archive_type)
                for xml_content in xml_files:
                    process_xml_content(f"{raw_url} (extracted from {archive_type})", xml_content)
            else:
                # Process as normal XML file
                # Try to parse the XML
                try:
                    root = etree.fromstring(file_content)
                    # Get the canonical form (C14N)
                    canonical_xml = etree.tostring(root, method="c14n")
                    # Hash the canonical XML
                    hash_value = hashlib.sha256(canonical_xml).hexdigest()
                    file_name_save = save / (hash_value + ".xml")
                    if not file_name_save.exists() and file_content and CheckValid(file_content):
                        print(f"{raw_url} is new")
                        with open(file_name_save, "wb") as f:
                            f.write(file_content)
                except etree.XMLSyntaxError:
                    # Not a valid XML file
                    continue
    
    return len(search_results["items"]) > 0  # Return True if there could be more results


# Function to fetch file content
def fetch_file_content(url: str):
    try:
        response = session.get(url)
        if response.status_code == 200:
            return response.content
        else:
            print(f"Failed to download {url}: {response.status_code}")
    except Exception as e:
        print(f"Error downloading {url}: {e}")
    return None


# Main execution
def main():
    # Create keys directory if it doesn't exist
    save.mkdir(exist_ok=True)
    
    # Fetch all pages
    page = 1
    has_more = True
    while has_more:
        has_more = fetch_and_process_results(page)
        page += 1

    # update cache
    open(cache_file, "w").writelines(cached_urls)

    # Validate existing files
    for file_path in save.glob("*.xml"):
        file_content = file_path.read_bytes()  # Read file content as bytes
        # Run CheckValid to determine if the file is still valid
        if not CheckValid(file_content):
            # Prompt user for deletion
            user_input = input(f"File '{file_path.name}' is no longer valid. Do you want to delete it? (y/N): ")
            if user_input.lower() == "y":
                try:
                    file_path.unlink()  # Delete the file
                    print(f"Deleted file: {file_path.name}")
                except OSError as e:
                    print(f"Error deleting file {file_path.name}: {e}")
            else:
                print(f"Kept file: {file_path.name}")


if __name__ == "__main__":
    main()
