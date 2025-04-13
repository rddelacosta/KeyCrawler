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
SEARCH_TERM = "<AndroidAttestation>"

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
        "duckduckgo": {"reset_time": None, "remaining": 20},  # Increased to 20
        "ecosia": {"reset_time": None, "remaining": 5}
    }

# Function to check and update rate limits
def check_rate_limit(source):
    global rate_limits
    
    now = datetime.now().isoformat()
    
    if rate_limits[source]["reset_time"] is None or now > rate_limits[source]["reset_time"]:
        if source == "github":
            rate_limits[source]["reset_time"] = (datetime.now() + timedelta(hours=1)).isoformat()
            rate_limits[source]["remaining"] = 30
        elif source == "duckduckgo":
            rate_limits[source]["reset_time"] = (datetime.now() + timedelta(minutes=15)).isoformat()
            rate_limits[source]["remaining"] = 20  # Reset to 20 for DuckDuckGo
        else:
            rate_limits[source]["reset_time"] = (datetime.now() + timedelta(minutes=15)).isoformat()
            rate_limits[source]["remaining"] = 5
    
    if rate_limits[source]["remaining"] <= 0:
        reset_time = datetime.fromisoformat(rate_limits[source]["reset_time"])
        wait_seconds = (reset_time - datetime.now()).total_seconds()
        if wait_seconds > 0:
            logger.info(f"Rate limit reached for {source}. Waiting {wait_seconds:.0f} seconds until reset.")
            return False
        else:
            if source == "github":
                rate_limits[source]["remaining"] = 30
            elif source == "duckduckgo":
                rate_limits[source]["remaining"] = 20
            else:
                rate_limits[source]["remaining"] = 5
            rate_limits[source]["reset_time"] = (datetime.now() + timedelta(minutes=15 if source != "github" else 60)).isoformat()
    
    rate_limits[source]["remaining"] -= 1
    
    with open(rate_limit_file, "w") as f:
        json.dump(rate_limits, f)
    
    return True

# Improved check_github_rate_limit function with built-in waiting option
def check_github_rate_limit(wait_if_low=False, min_required=10):
    """Check GitHub API rate limit with option to wait if limit is low."""
    try:
        rate_limit_url = "https://api.github.com/rate_limit"
        response = session.get(rate_limit_url)
        if response.status_code == 200:
            limit_data = response.json()
            remaining = limit_data.get('resources', {}).get('core', {}).get('remaining', 0)
            reset_time = limit_data.get('resources', {}).get('core', {}).get('reset', 0)
            current_time = int(time.time())
            wait_seconds = max(0, reset_time - current_time)
            
            logger.info(f"GitHub API requests remaining: {remaining}")
            logger.info(f"Rate limit resets in: {wait_seconds} seconds")
            
            # If we're asked to wait and the remaining count is below threshold
            if wait_if_low and remaining < min_required and wait_seconds > 0:
                # Only wait if it's a reasonable amount of time (less than 5 minutes)
                if wait_seconds < 300:
                    logger.info(f"Waiting {wait_seconds} seconds for rate limit to reset...")
                    time.sleep(wait_seconds + 5)  # Add 5 seconds buffer
                    # Recursively check again after waiting
                    return check_github_rate_limit(False)  # Don't wait again to avoid loops
                else:
                    logger.warning(f"Rate limit reset too far in future ({wait_seconds}s). Not waiting.")
            
            return {
                "remaining": remaining,
                "reset_in_seconds": wait_seconds,
                "reset_time": reset_time
            }
        else:
            logger.error(f"Failed to check rate limit status: {response.status_code}")
            return {"remaining": 0, "reset_in_seconds": 3600, "reset_time": current_time + 3600}
    except Exception as e:
        logger.error(f"Error checking rate limits: {e}")
        return {"remaining": 0, "reset_in_seconds": 3600, "reset_time": int(time.time()) + 3600}

# NEW: Discover GitHub repositories with improved rate limit handling
def discover_repositories(keywords=None, max_repos=10):  # Reduced to 10
    """Discover GitHub repositories using various methods."""
    if keywords is None:
        keywords = ["android", "attestation", "keybox"]  # Reduced keywords
    
    discovered_repos = []
    
    # Check GitHub rate limit status first
    try:
        rate_limit_info = check_github_rate_limit()
        remaining = rate_limit_info.get("remaining", 0)
        
        if remaining < 50:  # Need a safe buffer
            max_repos = min(max_repos, 3)  # Drastically reduce
        elif remaining < 100:
            max_repos = min(max_repos, 5)  # Reduce if moderately close to limit
            
        if remaining < 20:
            logger.warning("Too few API requests remaining. Limiting repository discovery.")
            return discovered_repos  # Return empty list to skip this step
    except Exception as e:
        logger.error(f"Error checking rate limits: {e}")
    
    # Method 1: Search for repositories by keywords (with fewer keywords)
    for keyword in keywords[:1]:  # Only use first keyword
        search_url = f"https://api.github.com/search/repositories?q={keyword}&sort=stars&order=desc"
        logger.info(f"Searching repositories with keyword: {keyword}")
        
        try:
            response = session.get(search_url)
            if response.status_code == 200:
                repos = response.json().get('items', [])
                for repo in repos:
                    repo_url = repo['html_url']
                    if repo_url not in discovered_repos:
                        discovered_repos.append(repo_url)
                        if len(discovered_repos) >= max_repos:
                            break
            elif response.status_code == 403:
                logger.error("Rate limit exceeded during repository discovery")
                break
            else:
                logger.error(f"Repository search failed: {response.status_code}")
        except Exception as e:
            logger.error(f"Error discovering repositories: {e}")
        
        time.sleep(5)  # Increased delay between searches
    
    logger.info(f"Discovered {len(discovered_repos)} repositories")
    return discovered_repos

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
                    file_list = zip_ref.namelist()
                    for file_name in file_list:
                        if file_name.lower().endswith('.xml'):
                            with zip_ref.open(file_name) as xml_file:
                                xml_content = xml_file.read()
                                xml_files.append((file_name, xml_content))
        
        elif archive_type == 'gzip':
            with io.BytesIO(content) as content_io:
                with gzip.GzipFile(fileobj=content_io) as gz_file:
                    extracted_content = gz_file.read()
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
        canonical_xml = etree.tostring(root, method="c14n")
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
    
    # Check rate limit before starting
    rate_limit_info = check_github_rate_limit()
    if rate_limit_info.get("remaining", 0) < 20:
        logger.warning(f"Only {rate_limit_info.get('remaining')} GitHub API requests remaining. Skipping GitHub search.")
        return
    
    for ext in SUPPORTED_EXTENSIONS:
        query = f"{SEARCH_TERM} extension:{ext[1:]}"
        search_url = f"https://api.github.com/search/code?q={query}"
        
        logger.info(f"Searching GitHub for: {query}")
        
        page = 1
        has_more = True
        while has_more:
            # Check rate limit again before each page
            if page > 1:
                rate_check = check_github_rate_limit()
                if rate_check.get("remaining", 0) < 10:
                    logger.warning(f"Only {rate_check.get('remaining')} GitHub API requests remaining. Stopping GitHub search.")
                    return
            
            params = {"per_page": 100, "page": page}
            response = session.get(search_url, params=params)
            
            if response.status_code == 403 and 'rate limit exceeded' in response.text.lower():
                logger.warning("GitHub API rate limit exceeded")
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
                    time.sleep(1)
                
                page += 1
                time.sleep(3)
                
            except Exception as e:
                logger.error(f"Error processing GitHub search results: {e}")
                has_more = False
            
            if "items" in search_results and len(search_results["items"]) < 100:
                has_more = False

# Enhanced process_repository function
def process_repository(repo_url):
    """Process a specific GitHub repository to find keybox files."""
    logger.info(f"Processing repository: {repo_url}")
    
    # Extract owner and repo name from URL
    parsed_url = urlparse(repo_url)
    path_parts = parsed_url.path.strip('/').split('/')
    if len(path_parts) < 2:
        logger.error(f"Invalid repository URL format: {repo_url}")
        return
        
    owner, repo = path_parts[0], path_parts[1]
    
    # Check rate limit before processing
    rate_limit_info = check_github_rate_limit()
    if rate_limit_info.get("remaining", 0) < 15:
        logger.error(f"Only {rate_limit_info.get('remaining')} GitHub API requests remaining. Skipping repository.")
        return
    
    # Try to access files directly with predictable paths first
    common_paths = [
        f"https://raw.githubusercontent.com/{owner}/{repo}/main/keybox.xml",
        f"https://raw.githubusercontent.com/{owner}/{repo}/master/keybox.xml",
        f"https://raw.githubusercontent.com/{owner}/{repo}/main/AndroidAttestation.xml",
        f"https://raw.githubusercontent.com/{owner}/{repo}/master/AndroidAttestation.xml",
        f"https://raw.githubusercontent.com/{owner}/{repo}/main/app/src/main/res/xml/keybox.xml",
        f"https://raw.githubusercontent.com/{owner}/{repo}/master/app/src/main/res/xml/keybox.xml",
        # Check zipball directory
        f"https://raw.githubusercontent.com/{owner}/{repo}/main/zipball/keybox.xml",
        f"https://raw.githubusercontent.com/{owner}/{repo}/master/zipball/keybox.xml",
    ]
    
    # Try common paths first (uses fewer API calls)
    for path in common_paths:
        try:
            logger.info(f"Trying direct file path: {path}")
            response = session.get(path, timeout=10)
            if response.status_code == 200:
                content = response.content
                if b'<?xml' in content[:100]:
                    process_xml_content(path, os.path.basename(path), content)
        except Exception as e:
            logger.debug(f"Error with direct path {path}: {e}")
    
    # Try common archive paths
    common_archives = [
        f"https://raw.githubusercontent.com/{owner}/{repo}/main/zipball/blackbox.tar",
        f"https://raw.githubusercontent.com/{owner}/{repo}/master/zipball/blackbox.tar",
        f"https://raw.githubusercontent.com/{owner}/{repo}/main/zipball/blackbox1.tar",
        f"https://raw.githubusercontent.com/{owner}/{repo}/master/zipball/blackbox1.tar",
        f"https://raw.githubusercontent.com/{owner}/{repo}/main/zipball/sanctuary.tar",
        f"https://raw.githubusercontent.com/{owner}/{repo}/master/zipball/sanctuary.tar",
    ]
    
    for archive_path in common_archives:
        try:
            logger.info(f"Trying direct archive path: {archive_path}")
            response = session.get(archive_path, timeout=20)
            if response.status_code == 200:
                content = response.content
                archive_type = is_archive(content)
                if archive_type:
                    process_archive(archive_path, content, archive_type)
        except Exception as e:
            logger.debug(f"Error with archive path {archive_path}: {e}")
    
    # Only scan the repository contents if we still have enough API calls
    rate_limit_info = check_github_rate_limit()
    if rate_limit_info.get("remaining", 0) < 10:
        logger.warning(f"Rate limit too low ({rate_limit_info.get('remaining')}). Skipping repository content listing.")
        return
        
    # Now try to get the repository contents
    try:
        contents_url = f"https://api.github.com/repos/{owner}/{repo}/contents"
        response = session.get(contents_url)
        
        if response.status_code != 200:
            logger.error(f"Failed to access repository contents: {response.status_code} - {response.text}")
            return
            
        process_repo_contents(response.json(), owner, repo)
    except Exception as e:
        logger.error(f"Error processing repository {repo_url}: {e}")

# Recursively process repository contents
def process_repo_contents(contents, owner, repo, path="", depth=0):
    """Recursively process repository contents."""
    # Limit recursion depth to avoid excessive API calls
    if depth > 1:  # Reduced from 2 to 1
        return
        
    if not isinstance(contents, list):
        contents = [contents]
        
    # Check for rate limit before processing contents
    rate_limit_info = check_github_rate_limit()
    if rate_limit_info.get("remaining", 0) < 10:
        logger.error(f"Only {rate_limit_info.get('remaining')} GitHub API requests remaining. Stopping repository processing.")
        return
        
    for item in contents:
        if item['type'] == 'dir':
            # Skip certain directories that are unlikely to contain keyboxes
            skip_dirs = ['node_modules', 'vendor', 'test', 'tests', 'doc', 'docs', 'example', 'examples']
            dir_name = item.get('name', '').lower()
            
            # Special case for zipball which might contain keyboxes
            if dir_name == 'zipball':
                logger.info(f"Found zipball directory, checking contents")
            elif dir_name in skip_dirs:
                logger.info(f"Skipping common directory: {dir_name}")
                continue
                
            try:
                # Recursively process directory
                dir_url = item['url']
                dir_response = session.get(dir_url)
                if dir_response.status_code == 200:
                    process_repo_contents(dir_response.json(), owner, repo, item['path'], depth + 1)
                else:
                    logger.error(f"Failed to access directory: {dir_url} - {dir_response.status_code}")
                    
                    # Check if we got rate limited
                    if dir_response.status_code == 403:
                        # Re-check rate limit
                        new_limit = check_github_rate_limit()
                        if new_limit.get("remaining", 0) < 10:
                            logger.error("Rate limit critical. Stopping directory processing.")
                            return
            except Exception as e:
                logger.error(f"Error processing directory {item.get('path', 'unknown')}: {e}")
                
        elif item['type'] == 'file':
            file_name = item['name'].lower()
            if any(file_name.endswith(ext) for ext in SUPPORTED_EXTENSIONS):
                # Construct raw URL for the file
                if path:
                    raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/main/{path}/{item['name']}"
                else:
                    raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/main/{item['name']}"
                
                logger.info(f"Found file with supported extension: {raw_url}")
                
                # Check if we've already processed this URL
                if raw_url + "\n" in cached_urls:
                    logger.info(f"Skipping already processed URL: {raw_url}")
                    continue
                
                # Process the URL
                process_url(raw_url)
                cached_urls.add(raw_url + "\n")
                time.sleep(1)  # Be nice to GitHub

# Extract URLs from search engine results
def extract_urls_from_html(html, search_engine):
    soup = BeautifulSoup(html, 'html.parser')
    result_urls = []
    
    if search_engine == "google":
        for result in soup.select('a[href^="/url?"]'):
            href = result.get('href')
            if href:
                url_params = parse_qs(urlparse(href).query)
                if 'q' in url_params:
                    url = url_params['q'][0]
                    if has_supported_extension(url):
                        result_urls.append(url)
    
    elif search_engine == "bing":
        for result in soup.select('a[href^="http"]'):
            href = result.get('href')
            if href and not href.startswith('https://www.bing.com/'):
                if has_supported_extension(href):
                    result_urls.append(href)
    
    elif search_engine == "duckduckgo":
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
        for result in soup.select('a.result-url'):
            href = result.get('href')
            if href and has_supported_extension(href):
                result_urls.append(href)
    
    return result_urls

# Function to search using search engines with rate limiting and pagination
def search_web():
    search_engines = [
        {
            "name": "Google",
            "id": "google",
            "url": "https://www.google.com/search",
            "params": lambda q, page: {"q": q, "num": 100, "start": (page - 1) * 100},
            "extractor": "google"
        },
        {
            "name": "Bing",
            "id": "bing",
            "url": "https://www.bing.com/search",
            "params": lambda q, page: {"q": q, "count": 50, "first": (page - 1) * 50},
            "extractor": "bing"
        },
        {
            "name": "DuckDuckGo",
            "id": "duckduckgo",
            "url": "https://duckduckgo.com/html/",
            "params": lambda q, page: {"q": q, "s": (page - 1) * 30},
            "extractor": "duckduckgo"
        },
        {
            "name": "Ecosia",
            "id": "ecosia",
            "url": "https://www.ecosia.org/search",
            "params": lambda q, page: {"q": q, "p": page - 1},
            "extractor": "ecosia"
        }
    ]
    
    max_pages = 2  # Reduced from 3 to 2 to respect rate limits
    
    for engine in search_engines:
        if not check_rate_limit(engine["id"]):
            logger.warning(f"{engine['name']} search rate limited. Skipping.")
            continue
            
        logger.info(f"Searching {engine['name']} for: {SEARCH_TERM}")
        
        try:
            session.headers.update({"User-Agent": random.choice(USER_AGENTS)})
            
            if engine["id"] == "duckduckgo":
                session.headers.update({
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "Upgrade-Insecure-Requests": "1"
                })
            
            for ext in [ext[1:] for ext in SUPPORTED_EXTENSIONS]:
                if engine["id"] == "duckduckgo":
                    query = f"{SEARCH_TERM} {ext}"
                elif engine["id"] == "ecosia":
                    query = f"{SEARCH_TERM} .{ext}"
                else:
                    query = f"{SEARCH_TERM} filetype:{ext}"
                
                for page in range(1, max_pages + 1):
                    if not check_rate_limit(engine["id"]):
                        logger.warning(f"Rate limit hit on {engine['name']} page {page}. Moving to next engine.")
                        break
                    
                    response = session.get(
                        engine["url"],
                        params=engine["params"](query, page),
                        timeout=15
                    )
                    
                    if response.status_code == 200:
                        urls = extract_urls_from_html(response.text, engine["extractor"])
                        for url in urls:
                            process_url(url)
                            time.sleep(random.uniform(1.5, 3.5))
                    else:
                        logger.error(f"{engine['name']} page {page} failed: {response.status_code}")
                        break
                    
                    time.sleep(random.uniform(5, 10))
                
                time.sleep(random.uniform(5, 10))
        
        except Exception as e:
            logger.error(f"Error in {engine['name']} search: {e}")
        
        time.sleep(random.uniform(10, 15))

# Check if a URL has a supported file extension
def has_supported_extension(url):
    parsed_url = urlparse(url)
    path = parsed_url.path.lower()
    return any(path.endswith(ext) for ext in SUPPORTED_EXTENSIONS)

# Function to process a URL
def process_url(url):
    if not has_supported_extension(url):
        return
        
    logger.info(f"Processing URL: {url}")
    
    if url + "\n" in cached_urls:
        return
        
    cached_urls.add(url + "\n")
    
    try:
        session.headers.update({"User-Agent": random.choice(USER_AGENTS)})
        response = session.get(url, timeout=20)
        
        if response.status_code != 200:
            logger.error(f"Failed to download {url}: {response.status_code}")
            return
            
        content = response.content
        
        archive_type = is_archive(content)
        if archive_type:
            process_archive(url, content, archive_type)
            return
            
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
    save.mkdir(exist_ok=True)
    
    logger.info("Starting KeyBoxer search (XML and archives only)")
    
    try:
        # Track whether we attempted each search method
        methods_attempted = {
            "specific_repos": False,
            "discovered_repos": False,
            "github_search": False,
            "web_search": False
        }
        
        # FIRST: Check GitHub rate limit before doing ANY GitHub operations
        rate_limit_info = check_github_rate_limit()
        remaining = rate_limit_info.get("remaining", 0)
        
        # If rate limit is critically low (less than 20), skip ALL GitHub API operations
        if remaining < 20:
            logger.warning(f"CRITICAL: GitHub API rate limit too low ({remaining} remaining). Skipping ALL GitHub API operations.")
            logger.warning("Moving directly to web search which doesn't use GitHub API.")
            methods_attempted["specific_repos"] = True  # Mark as attempted
            methods_attempted["discovered_repos"] = True  # Mark as attempted  
            methods_attempted["github_search"] = True  # Mark as attempted
        else:
            # Only proceed with GitHub operations if we have enough quota
            
            # Add specific repositories to directly scan
            target_repos = [
                "https://github.com/Citra-Standalone/Citra-Standalone"
            ]
            
            # Process each target repository
            logger.info(f"Processing {len(target_repos)} specific repositories")
            for repo_url in target_repos:
                process_repository(repo_url)
                time.sleep(30)
            
            methods_attempted["specific_repos"] = True
            
            # Recheck rate limit after specific repos
            rate_limit_info = check_github_rate_limit()
            remaining = rate_limit_info.get("remaining", 0)
                    
            if remaining < 20:
                logger.warning(f"Rate limit too low after specific repos ({remaining} remaining). Skipping discovery and GitHub search.")
                methods_attempted["discovered_repos"] = True
                methods_attempted["github_search"] = True
            else:
                # Discover and process additional repositories
                try:
                    discovered_repos = discover_repositories(max_repos=5)  # Very conservative limit
                    logger.info(f"Processing {len(discovered_repos)} discovered repositories")
                    
                    for repo_count, repo_url in enumerate(discovered_repos, 1):
                        # Check rate limit before EACH repository
                        rate_limit_info = check_github_rate_limit()
                        remaining = rate_limit_info.get("remaining", 0)
                        
                        if remaining < 15:
                            logger.warning(f"Rate limit too low ({remaining}). Stopping repository processing.")
                            break
                            
                        logger.info(f"Processing repository {repo_count}/{len(discovered_repos)}: {repo_url}")
                        process_repository(repo_url)
                        time.sleep(60)
                
                    methods_attempted["discovered_repos"] = True
                except Exception as e:
                    logger.error(f"Error in repository discovery: {e}")
                
                # Recheck rate limit before GitHub code search
                rate_limit_info = check_github_rate_limit()
                remaining = rate_limit_info.get("remaining", 0)
                
                if remaining < 15:
                    logger.warning(f"Rate limit too low ({remaining}). Skipping GitHub code search.")
                    methods_attempted["github_search"] = True
                else:
                    try:
                        logger.info("Starting GitHub API search.")
                        search_github()
                        methods_attempted["github_search"] = True
                    except Exception as e:
                        logger.error(f"Error in GitHub search: {e}")
        
        # Always perform web search regardless of GitHub API rate limits
        try:
            logger.info("Starting web search (independent of GitHub API).")
            search_web()
            methods_attempted["web_search"] = True
        except Exception as e:
            logger.error(f"Error in web search: {e}")
        
        # Always save the cache and validate files
        logger.info("Saving cache and validating files...")
        with open(cache_file, "w") as f:
            f.writelines(cached_urls)
        
        valid_files = 0
        for file_path in save.glob("*.xml"):
            try:
                file_content = file_path.read_bytes()
                if CheckValid(file_content):
                    valid_files += 1
                else:
                    logger.warning(f"File '{file_path.name}' is not valid.")
            except Exception as e:
                logger.error(f"Error validating file {file_path}: {e}")
        
        logger.info(f"KeyBoxer completed. Found {valid_files} valid keybox files.")
    
    except KeyboardInterrupt:
        logger.info("Search interrupted by user")
    except Exception as e:
        logger.error(f"Error during search: {e}")
    finally:
        # Save progress even on error
        try:
            with open(rate_limit_file, "w") as f:
                json.dump(rate_limits, f)
            
            # Count files found
            file_count = len(list(save.glob("*.xml")))
            logger.info(f"Found {file_count} keybox files in total.")
            
            # Create a summary file
            with open("keyboxer_summary.txt", "w") as f:
                f.write(f"KeyBoxer Summary\n")
                f.write(f"===============\n")
                f.write(f"Run completed at: {datetime.now().isoformat()}\n")
                f.write(f"Total keybox files found: {file_count}\n")
                f.write(f"Methods attempted:\n")
                for method, attempted in methods_attempted.items():
                    f.write(f"- {method}: {'Yes' if attempted else 'No'}\n")
        except Exception as summary_error:
            logger.error(f"Error creating summary: {summary_error}")
        
        logger.info("KeyBoxer completed")