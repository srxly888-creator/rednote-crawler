import sys
import os
import json

# For demo purposes, we add the project root to sys.path
# In a real scenario, you would 'pip install xhs-drission-crawler'
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(os.path.dirname(current_dir))
sys.path.insert(0, project_root)

from crawler import XHSCrawler

def main():
    print("🚀 Starting Library Demo...")
    
    # Initialize crawler (headless for demo).
    # Tip: if you need to login/scan QR code, run with headless=False for the first time.
    crawler = XHSCrawler(headless=True, port=9224)
    
    try:
        # Example 1: Search for notes
        keyword = "Python"
        print(f"🔍 Searching for '{keyword}'...")
        
        count = 0
        # We only take the first 3 results for this demo
        search_generator = crawler.start_search_crawling(keyword=keyword, start_page=1, note_type=0)
        
        for note in search_generator:
            note_id = note.get("id") or note.get("note_id")
            card = note.get("note_card", {}) or {}
            title = card.get("display_title") or card.get("title") or ""
            print(f"  - [{note_id}] {title}")
            count += 1
            if count >= 3:
                break
        
        crawler.stop()
        
    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        crawler.close()
        print("✅ Demo finished.")

if __name__ == "__main__":
    main()
