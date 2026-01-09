import sys
import os
from pathlib import Path

# Add project root to sys.path
# If this file is in crawler/verify_comments.py, the root is one level up (Documents/xhs_crawler_system)
project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root))

from crawler import XHSCrawler
from loguru import logger

class TestXHSCrawler(XHSCrawler):
    def check_and_wait_for_login(self):
        logger.info("Skipping strict login check for verification...")
        return

def verify_comments():
    logger.info("Starting verification script...")
    # Use headless=False as requested, and try to load local cookies
    cookie_file = project_root / "cookies.json"
    logger.info(f"Using cookie file: {cookie_file}")
    # Use our subclass
    crawler = TestXHSCrawler(headless=False, port=9333, cookie_path=str(cookie_file))
    
    try:
        # Check login first (cookies might be needed)
        crawler.check_login_status()
        
        # Search for a note - using a reliable keyword
        logger.info("Searching for 'Python'...")
        found_note_id = None
        
        # Just grab the first note from search results
        for item in crawler.start_search_crawling(keyword="Python", start_page=1):
            if item.get('model_type') == 'note':
                found_note_id = item.get('id')
                logger.info(f"Found note ID: {found_note_id}")
                break
        
        if not found_note_id:
            logger.error("No note found! Cannot verify.")
            return

        logger.info(f"Scraping note detail for {found_note_id}...")
        detail = crawler.scrape_note_detail(found_note_id)
        
        comments = detail.get("comments", [])
        logger.info(f"Scraped {len(comments)} comments.")
        
        print("\n--- Comment Verification ---")
        for i, c in enumerate(comments[:5]):
            print(f"Comment #{i+1}:")
            print(f"  User: {c.get('user')}")
            print(f"  Content: {c.get('content')[:30]}...")
            print(f"  ID: {c.get('id')} (Expected: non-empty string)")
            print(f"  Date: {c.get('date')} (Expected: non-empty string)")
            print(f"  Like Count: {c.get('like_count')} (Expected: string/number or None if not implemented)")
            print(f"  Location: {c.get('ip_location')} (Expected: string or None)")
            print("-" * 30)
            
            # Basic validation
            if not c.get('id'):
                logger.warning(f"Comment #{i+1} MISSING ID")
            if not c.get('date'):
                logger.warning(f"Comment #{i+1} MISSING DATE")

    except Exception as e:
        logger.exception("An error occurred during verification")
    finally:
        crawler.close()

if __name__ == "__main__":
    verify_comments()
