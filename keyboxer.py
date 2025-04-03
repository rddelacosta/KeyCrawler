import requests
import hashlib
import os
import zipfile
import gzip
import io
import tarfile
import time
import random
import json
from urllib.parse import urljoin, urlparse, parse_qs
from bs4 import BeautifulSoup
import logging
from datetime import datetime, timedelta

from lxml import etree
from pathlib import Path
from dotenv import load_dotenv

from check import keybox_check as CheckValid

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("keyboxer.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("keyboxer")

# Load environment variables from .env file
load_dotenv()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

# User agents for rotating to avoid detection
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
]

# Set up session
session = requests.Session()
session.headers.update({
    "User-Agent": random.choice(USER_AGENTS),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5"
})

if GITHUB_TOKEN:
    session.headers.update({"Authorization": f"token {GITHUB_TOKEN}"})

# Use only the original search term
SEARCH_TERM = "AndroidAttestation"

# Supported file extensions
SUPPORTED_EXTENSIONS = ['.xml', '.zip', '.gz', '.tar', '.tgz', '.tar.gz']

# File paths
save = Path(__file__).resolve().parent / "keys"
cache_file = Path(__file__).resolve().parent / "cache.txt"
rate_limit_file = Path(__file__).resolve().parent / "rate_limits.json"

# Load cache
try:
    cached_urls = set(open(cache_file, "r").readlines())
except FileNotFoundError:
    cached_urls = set()

# Load rate limits
try:
    with open(rate_limit_file, "r") as f:
        rate_limits = json.load(f)
except FileNotFoundError:
    rate_limits = {
        "github": {"reset_time": None, "remaining": 0},
        "google": {"reset_time": None, "remaining": 5},
        "bing": {"reset_time": None, "remaining": 5},
        "duckduckgo": {"reset_time": None, "remaining": 5},
        "ecosia": {"reset_time": None, "remaining": 5}
    }

# Function to check and update rate limits
def check_rate_limit(source):
    global rate_limits
    
    now = datetime.now().isoformat()
    
    # If reset time has passed, reset the counters
    if rate_limits[source]["reset_time"] is None or now > rate_limits[source]["reset_time"]:
        if source == "github":
            rate_limits[source]["reset_time"] = (datetime.now() + timedelta(hours=1)).isoformat()
            rate_limits[source]["remaining"] = 30  # GitHub has 30 searches per hour
        else:
            rate_limits[source]["reset_time"] = (datetime.now() + timedelta(minutes=15)).isoformat()
            rate_limits[source]["remaining"] = 5  # Allow 5 searches per 15 minutes for search engines
    
    # Check if we have any remaining searches
    if rate_limits[source]["remaining"] <= 0:
        reset_time = datetime.fromisoformat(rate_limits[source]["reset_time"])
        wait_seconds = (reset_time - datetime.now()).total_seconds()
        
        if wait_seconds > 0:
            logger.info(f"Rate limit reached for {source}. Waiting {wait_seconds:.0f} seconds until reset.")
            return False
        else:
            # Reset has passed
            if source == "github":
                rate_limits[source]["reset_time"] = (datetime.now() + timedelta(hours=1)).isoformat()
                rate_limits[source]["remaining"] = 30
            else:
                rate_limits[source]["reset_time"] = (datetime.now() + timedelta(minutes=15)).isoformat()
                rate_limits[source]["remaining"] = 5
    
    # Decrement the counter
    rate_limits[source]["remaining"] -= 1
    
    # Save updated rate limits
    with open(rate_limit_file, "w") as f:
        json.dump(rate_limits, f)
    
    return True

# Function to check if content is a valid archive format
def is_archive(content):
    try:
        if content.startswith(b'PK\x03\x04'):
            return 'zip'
        elif content.startswith(b'\x1f\x8b'):
            return 'gzip'
        elif len(content) > 257+5 and content[257:257+5] == b'ustar':
            return 'tar'
    except:
        pass
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
                                xml_files.append((file_name, xml_content))
        
        elif archive_type == 'gzip':
            with io.BytesIO(content) as content_io:
                with gzip.GzipFile(fileobj=content_io) as gz_file:
                    extracted_content = gz_file.read()
                    # Check if the extracted content is XML
                    if extracted_content.startswith(b'<?xml'):
                        xml_files.append(("extracted.xml", extracted_content))
        
        elif archive_type == 'tar':
            with io.BytesIO(content) as content_io:
                with tarfile.open(fileobj=content_io, mode='r') as tar_ref:
                    for member in tar_ref.getmembers():
                        if member.name.lower().endswith('.xml'):
                            f = tar_ref.extractfile(member)
                            if f:
                                xml_content = f.read()
                                xml_files.append((member.name, xml_content))
    
    except Exception as e:
        logger.error(f"Error extracting from archive: {e}")
    
    return xml_files

# Function to process XML content
def process_xml_content(url, file_name, content):
    try:
        root = etree.fromstring(content)
        # Get the canonical form (C14N)
        canonical_xml = etree.tostring(root, method="c14n")
        # Hash the canonical XML
        hash_value = hashlib.sha256(canonical_xml).hexdigest()
        file_name_save = save / (hash_value + ".xml")
        
        if not file_name_save.exists() and CheckValid(content):
            logger.info(f"Found new valid XML: {url}/{file_name}")
            with open(file_name_save, "wb") as f:
                f.write(content)
            return True
    except Exception as e:
        logger.error(f"Error processing XML content from {url}/{file_name}: {e}")
    return False

# Function to search on GitHub with rate limit handling
def search_github():
    if not GITHUB_TOKEN:
        logger.warning("No GitHub token provided, skipping GitHub search")
        return
    
    if not check_rate_limit("github"):
        logger.warning("GitHub search rate limited. Skipping.")
        return
    
    # Search for files with specific extensions using AndroidAttestation
    for ext in SUPPORTED_EXTENSIONS:
        query = f"{SEARCH_TERM} extension:{ext[1:]}"
        search_url = f"https://api.github.com/search/code?q={query}"
        
        logger.info(f"Searching GitHub for: {query}")
        
        page = 1
        has_more = True
        while has_more:
            params = {"per_page": 100, "page": page}
            response = session.get(search_url, params=params)
            
            if response.status_code == 403 and 'rate limit exceeded' in response.text.lower():
                logger.warning("GitHub API rate limit exceeded")
                # Update rate limit info
                rate_limits["github"]["remaining"] = 0
                with open(rate_limit_file, "w") as f:
                    json.dump(rate_limits, f)
                return
            
            if response.status_code != 200:
                logger.error(f"GitHub search failed: {response.status_code} - {response.text}")
                time.sleep(5)
                break
                
            try:
                search_results = response.json()
                
                if "items" not in search_results or len(search_results["items"]) == 0:
                    has_more = False
                    continue
                    
                for item in search_results["items"]:
                    raw_url = (
                        item["html_url"]
                        .replace("github.com", "raw.githubusercontent.com")
                        .replace("/blob/", "/")
                    )
                    
                    if raw_url + "\n" in cached_urls:
                        continue
                        
                    process_url(raw_url)
                    cached_urls.add(raw_url + "\n")
                    
                    # Sleep to avoid rate limiting
                    time.sleep(1)
                
                page += 1
                
                # Sleep between pages
                time.sleep(3)
                
            except Exception as e:
                logger.error(f"Error processing GitHub search results: {e}")
                has_more = False
            
            # Check if we've reached the last page
            if "items" in search_results and len(search_results["items"]) < 100:
                has_more = False

# Extract URLs from search engine results
def extract_urls_from_html(html, search_engine):
    soup = BeautifulSoup(html, 'html.parser')
    result_urls = []
    
    if search_engine == "google":
        # Google search results
        for result in soup.select('a[href^="/url?"]'):
            href = result.get('href')
            if href:
                url_params = parse_qs(urlparse(href).query)
                if 'q' in url_params:
                    url = url_params['q'][0]
                    if has_supported_extension(url):
                        result_urls.append(url)
    
    elif search_engine == "bing":
        # Bing search results
        for result in soup.select('a[href^="http"]'):
            href = result.get('href')
            if href and not href.startswith('https://www.bing.com/'):
                if has_supported_extension(href):
                    result_urls.append(href)
    
    elif search_engine == "duckduckgo":
        # DuckDuckGo search results
        for result in soup.select('a.result__a'):
            href = result.get('href')
            if href:
                try:
                    url_params = parse_qs(urlparse(href).query)
                    if 'uddg' in url_params:
                        url = url_params['uddg'][0]
                        if has_supported_extension(url):
                            result_urls.append(url)
                except:
                    pass
    
    elif search_engine == "ecosia":
        # Ecosia search results
        for result in soup.select('a.result-url'):
            href = result.get('href')
            if href and has_supported_extension(href):
                result_urls.append(href)
    
    return result_urls

# Function to search using search engines with rate limiting
def search_web():
    search_engines = [
        {
            "name": "Google",
            "id": "google",
            "url": "https://www.google.com/search",
            "params": lambda q: {"q": q, "num": 100},
            "extractor": "google"
        },
        {
            "name": "Bing",
            "id": "bing",
            "url": "https://www.bing.com/search",
            "params": lambda q: {"q": q, "count": 50},
            "extractor": "bing"
        },
        {
            "name": "DuckDuckGo",
            "id": "duckduckgo",
            "url": "https://duckduckgo.com/html/",
            "params": lambda q: {"q": q},
            "extractor": "duckduckgo"
        },
        {
            "name": "Ecosia",
            "id": "ecosia",
            "url": "https://www.ecosia.org/search",
            "params": lambda q: {"q": q},
            "extractor": "ecosia"
        }
    ]
    
    # For each search engine
    for engine in search_engines:
        # Check rate limits
        if not check_rate_limit(engine["id"]):
            logger.warning(f"{engine['name']} search rate limited. Skipping.")
            continue
            
        logger.info(f"Searching {engine['name']} for: {SEARCH_TERM}")
        
        # Search for XML files
        try:
            session.headers.update({"User-Agent": random.choice(USER_AGENTS)})
            
            # Add special headers for DuckDuckGo
            if engine["id"] == "duckduckgo":
                session.headers.update({
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "Upgrade-Insecure-Requests": "1"
                })
            
            # Create search query specifically for XML and archives
            for ext in [ext[1:] for ext in SUPPORTED_EXTENSIONS]:
                query = f"{SEARCH_TERM} filetype:{ext}"
                response = session.get(
                    engine["url"],
                    params=engine["params"](query),
                    timeout=15
                )
                
                if response.status_code == 200:
                    # Extract URLs from the search results
                    urls = extract_urls_from_html(response.text, engine["extractor"])
                    
                    for url in urls:
                        process_url(url)
                        # Add delay between requests
                        time.sleep(random.uniform(1.5, 3.5))
                
                # Add delay between different file type searches
                time.sleep(random.uniform(5, 10))
        
        except Exception as e:
            logger.error(f"Error in {engine['name']} search: {e}")
        
        # Add delay between search engines
        time.sleep(random.uniform(10, 15))

# Check if a URL has a supported file extension
def has_supported_extension(url):
    parsed_url = urlparse(url)
    path = parsed_url.path.lower()
    
    return any(path.endswith(ext) for ext in SUPPORTED_EXTENSIONS)

# Function to process a URL
def process_url(url):
    # Skip if not a supported file type
    if not has_supported_extension(url):
        return
        
    logger.info(f"Processing URL: {url}")
    
    if url + "\n" in cached_urls:
        return
        
    cached_urls.add(url + "\n")
    
    try:
        session.headers.update({"User-Agent": random.choice(USER_AGENTS)})
        response = session.get(url, timeout=10)
        
        if response.status_code != 200:
            logger.error(f"Failed to download {url}: {response.status_code}")
            return
            
        content = response.content
        
        # Check if it's an archive
        archive_type = is_archive(content)
        if archive_type:
            process_archive(url, content, archive_type)
            return
            
        # Check if it's an XML file
        if url.lower().endswith('.xml') or b'<?xml' in content[:100]:
            process_xml_content(url, os.path.basename(urlparse(url).path), content)
            
    except Exception as e:
        logger.error(f"Error processing URL {url}: {e}")

# Function to process an archive file
def process_archive(url, content, archive_type):
    logger.info(f"Processing {archive_type} archive from {url}")
    xml_files = extract_xml_from_archive(content, archive_type)
    
    for file_name, xml_content in xml_files:
        process_xml_content(url, file_name, xml_content)

# Main execution
def main():
    # Create keys directory if it doesn't exist
    save.mkdir(exist_ok=True)
    
    logger.info("Starting KeyBoxer search (XML and archives only)")
    
    try:
        # Search GitHub repositories
        search_github()
        
        # Search the web using search engines
        search_web()
        
        # Update cache
        with open(cache_file, "w") as f:
            f.writelines(cached_urls)
        
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
                        logger.info(f"Deleted file: {file_path.name}")
                    except OSError as e:
                        logger.error(f"Error deleting file {file_path.name}: {e}")
                else:
                    logger.info(f"Kept file: {file_path.name}")
    
    except KeyboardInterrupt:
        logger.info("Search interrupted by user")
    except Exception as e:
        logger.error(f"Error during search: {e}")
    finally:
        # Save the final state of rate limits
        with open(rate_limit_file, "w") as f:
            json.dump(rate_limits, f)
        
        logger.info("KeyBoxer completed")

if __name__ == "__main__":
    main()
