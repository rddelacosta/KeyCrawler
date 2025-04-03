import requests
import hashlib
import os
import zipfile
import gzip
import io
import re
import tarfile
import time
import random
from urllib.parse import urljoin, urlparse, parse_qs, urlencode
from bs4 import BeautifulSoup
import logging

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
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0"
]

# Set up session
session = requests.Session()
session.headers.update({
    "User-Agent": random.choice(USER_AGENTS),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive",
})

if GITHUB_TOKEN:
    session.headers.update({"Authorization": f"token {GITHUB_TOKEN}"})

# Search terms related to keybox and attestation
SEARCH_TERMS = [
    "AndroidAttestation",
    "keybox.xml",
    "keybox xml attestation",
    "attestation xml",
    "keystore attestation xml",
]

# File paths
save = Path(__file__).resolve().parent / "keys"
cache_file = Path(__file__).resolve().parent / "cache.txt"
visited_urls_file = Path(__file__).resolve().parent / "visited_urls.txt"

# Load cache and visited URLs
try:
    cached_urls = set(open(cache_file, "r").readlines())
except FileNotFoundError:
    cached_urls = set()
    
try:
    visited_urls = set(open(visited_urls_file, "r").readlines())
except FileNotFoundError:
    visited_urls = set()


# Function to check if content is a valid archive format
def is_archive(content):
    try:
        # Try to identify ZIP files
        if content.startswith(b'PK\x03\x04'):
            return 'zip'
        # Try to identify GZIP files
        elif content.startswith(b'\x1f\x8b'):
            return 'gzip'
        # Try to identify TAR files (less reliable header check)
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
                    if extracted_content.startswith(b'<?xml') or b'<AndroidAttestation>' in extracted_content:
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
    
    except (zipfile.BadZipFile, gzip.BadGzipFile, tarfile.ReadError, Exception) as e:
        logger.error(f"Error extracting from archive: {e}")
    
    return xml_files


# Function to process XML content
def process_xml_content(url, file_name, content):
    try:
        if b'<AndroidAttestation>' not in content:
            return False
            
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
    except etree.XMLSyntaxError:
        pass
    except Exception as e:
        logger.error(f"Error processing XML content from {url}/{file_name}: {e}")
    return False


# Function to search on GitHub
def search_github():
    if not GITHUB_TOKEN:
        logger.warning("No GitHub token provided, skipping GitHub search")
        return
        
    for search_term in SEARCH_TERMS:
        search_url = f"https://api.github.com/search/code?q={search_term}"
        
        logger.info(f"Searching GitHub for: {search_term}")
        
        page = 1
        has_more = True
        while has_more:
            params = {"per_page": 100, "page": page}
            response = session.get(search_url, params=params)
            
            if response.status_code != 200:
                logger.error(f"GitHub search failed: {response.status_code} - {response.text}")
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
                time.sleep(2)
                
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
                    result_urls.append(url_params['q'][0])
    
    elif search_engine == "bing":
        # Bing search results
        for result in soup.select('a[href^="http"]'):
            href = result.get('href')
            if href and not href.startswith('https://www.bing.com/'):
                result_urls.append(href)
    
    elif search_engine == "duckduckgo":
        # DuckDuckGo search results
        for result in soup.select('a.result__a'):
            href = result.get('href')
            if href:
                # DuckDuckGo uses relative URLs with a redirect
                try:
                    url_params = parse_qs(urlparse(href).query)
                    if 'uddg' in url_params:
                        result_urls.append(url_params['uddg'][0])
                except:
                    pass
    
    elif search_engine == "ecosia":
        # Ecosia search results
        for result in soup.select('a.result-url'):
            href = result.get('href')
            if href:
                result_urls.append(href)
    
    return result_urls


# Function to search using search engines
def search_web():
    search_engines = [
        {
            "name": "Google",
            "url": "https://www.google.com/search",
            "params": lambda q: {"q": q, "num": 100},
            "extractor": "google"
        },
        {
            "name": "Bing",
            "url": "https://www.bing.com/search",
            "params": lambda q: {"q": q, "count": 50},
            "extractor": "bing"
        },
        {
            "name": "DuckDuckGo",
            "url": "https://duckduckgo.com/html/",
            "params": lambda q: {"q": q},
            "extractor": "duckduckgo"
        },
        {
            "name": "Ecosia",
            "url": "https://www.ecosia.org/search",
            "params": lambda q: {"q": q},
            "extractor": "ecosia"
        }
    ]
    
    for search_term in SEARCH_TERMS:
        for engine in search_engines:
            logger.info(f"Searching {engine['name']} for: {search_term}")
            
            try:
                # Rotate user agent for each search
                session.headers.update({"User-Agent": random.choice(USER_AGENTS)})
                
                # Add special headers for DuckDuckGo
                if engine["name"] == "DuckDuckGo":
                    session.headers.update({
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                        "Sec-Fetch-Dest": "document",
                        "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-Site": "none",
                        "Sec-Fetch-User": "?1",
                        "Upgrade-Insecure-Requests": "1"
                    })
                
                search_query = f"{search_term} filetype:xml OR filetype:zip"
                response = session.get(
                    engine["url"],
                    params=engine["params"](search_query),
                    timeout=15
                )
                
                if response.status_code != 200:
                    logger.error(f"{engine['name']} search failed: {response.status_code}")
                    continue
                
                # Extract URLs from the search results
                urls = extract_urls_from_html(response.text, engine["extractor"])
                
                for url in urls:
                    parsed_url = urlparse(url)
                    # Skip search engine domains
                    if parsed_url.netloc in [
                        "google.com", "www.google.com", 
                        "bing.com", "www.bing.com",
                        "duckduckgo.com", "www.duckduckgo.com",
                        "ecosia.org", "www.ecosia.org"
                    ]:
                        continue
                    
                    process_url(url)
                    
                    # Sleep to avoid rate limiting
                    time.sleep(2)
            
            except Exception as e:
                logger.error(f"Error in {engine['name']} search: {e}")
            
            # Sleep between search engines
            time.sleep(5)


# Function to crawl a website for links
def crawl_website(url, depth=1, max_pages=50):
    if depth <= 0 or max_pages <= 0:
        return
        
    # Extract base URL for resolving relative links
    parsed_url = urlparse(url)
    base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
    
    # Check if URL has been visited
    if url + "\n" in visited_urls:
        return
        
    visited_urls.add(url + "\n")
    
    try:
        session.headers.update({"User-Agent": random.choice(USER_AGENTS)})
        response = session.get(url, timeout=10)
        
        if response.status_code != 200:
            return
            
        # Check if the content is XML
        content_type = response.headers.get('Content-Type', '').lower()
        if 'xml' in content_type:
            process_url(url)
            return
            
        # Check if the content is an archive
        content = response.content
        archive_type = is_archive(content)
        if archive_type:
            process_archive(url, content, archive_type)
            return
            
        # Only parse HTML for more links
        if 'html' not in content_type:
            return
            
        soup = BeautifulSoup(response.text, 'html.parser')
        links = soup.find_all('a', href=True)
        
        pages_crawled = 1
        for link in links:
            if pages_crawled >= max_pages:
                break
                
            href = link['href']
            
            # Skip empty links, anchors, and JavaScript links
            if not href or href.startswith('#') or href.startswith('javascript:'):
                continue
                
            # Resolve relative URLs
            if not href.startswith('http'):
                href = urljoin(base_url, href)
                
            # Skip external domains
            href_parsed = urlparse(href)
            if href_parsed.netloc != parsed_url.netloc:
                continue
                
            # Skip if already visited
            if href + "\n" in visited_urls:
                continue
                
            # Check if it's an XML or archive file
            if href.lower().endswith(('.xml', '.zip', '.gz', '.tar')):
                process_url(href)
                pages_crawled += 1
            else:
                # Recursively crawl the link
                crawl_website(href, depth - 1, max_pages - pages_crawled)
                pages_crawled += 1
            
            # Sleep to avoid hammering the server
            time.sleep(1)
            
    except Exception as e:
        logger.error(f"Error crawling {url}: {e}")


# Function to process a URL
def process_url(url):
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
            process_xml_content(url, "direct.xml", content)
            
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
    
    logger.info("Starting KeyBoxer web search")
    
    # Search GitHub repositories
    search_github()
    
    # Search the web using search engines
    search_web()
    
    # Update cache and visited URLs
    with open(cache_file, "w") as f:
        f.writelines(cached_urls)
    
    with open(visited_urls_file, "w") as f:
        f.writelines(visited_urls)
    
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
    
    logger.info("KeyBoxer completed")


if __name__ == "__main__":
    main()
