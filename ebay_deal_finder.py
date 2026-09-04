import os
import sys
import time
import base64
import smtplib
from email.mime.text import MIMEText
try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv(*a, **kw):  # fallback if python-dotenv not installed
        return False
try:
    import openai
except ModuleNotFoundError:
    print("Missing 'openai' - run with venv: source venv/bin/activate && pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)
try:
    import requests
except ModuleNotFoundError:
    print("Missing 'requests' - run with venv: source venv/bin/activate && pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)
import json
import re
import argparse

# Load environment variables
load_dotenv()

CLIENT_ID = os.getenv("EBAY_CLIENT_ID")
CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
LLM_MODEL_NAME = os.getenv("LLM_MODEL_NAME", "mimo-v2.5-free")

# Set up OpenAI client (OpenCode API key should work seamlessly with the OpenAI package)
# Using OpenCode Zen gateway
client = openai.OpenAI(
    api_key=OPENAI_API_KEY,
    base_url="https://opencode.ai/zen/v1"
)

# Parse command line arguments
parser = argparse.ArgumentParser(description="eBay Deal Finder Daemon")
parser.add_argument("query", nargs='?', default="MacBook Pro M3", help="Search query")
parser.add_argument("--max-price", type=float, help="Maximum price (USD)")
parser.add_argument("--category-id", type=str, help="eBay Category ID (e.g. 177 for Laptops)")
parser.add_argument("--extra-prompt", type=str, default="", help="Extra instructions for the LLM")
parser.add_argument("--once", action="store_true", help="Run only once and exit")
args = parser.parse_args()

SEARCH_QUERY = args.query
MAX_PRICE = args.max_price
CATEGORY_ID = args.category_id
EXTRA_PROMPT = args.extra_prompt
RUN_ONCE = args.once

POLL_INTERVAL_SECONDS = 300  # 5 minutes
SEEN_FILE = os.path.join(os.path.dirname(__file__), ".seen_items.json")
SEEN_ITEMS = set()
TOKEN_CACHE = {"token": None, "expiry": 0}

# Vision-capable models (allowlist, not denylist)
VISION_MODELS = {"mimo", "gpt-4", "gemini", "claude", "vision"}

def load_seen():
    global SEEN_ITEMS
    try:
        if os.path.exists(SEEN_FILE):
            with open(SEEN_FILE, "r") as f:
                SEEN_ITEMS = set(json.load(f))
    except Exception:
        SEEN_ITEMS = set()

def save_seen():
    try:
        # cap to last 5000 to avoid unbounded growth
        data = list(SEEN_ITEMS)[-5000:]
        with open(SEEN_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"Failed to save seen items: {e}")

def validate_env():
    missing = []
    if not CLIENT_ID:
        missing.append("EBAY_CLIENT_ID")
    if not CLIENT_SECRET:
        missing.append("EBAY_CLIENT_SECRET")
    if not OPENAI_API_KEY:
        missing.append("OPENAI_API_KEY")
    if missing:
        print(f"Missing required env vars: {', '.join(missing)}")
        sys.exit(1)

def supports_vision(model_name):
    name = model_name.lower()
    return any(v in name for v in VISION_MODELS)

def get_ebay_token(force_refresh=False):
    """Gets an OAuth token from eBay - cached 2h, validates env."""
    now = time.time()
    if not force_refresh and TOKEN_CACHE["token"] and now < TOKEN_CACHE["expiry"] - 60:
        return TOKEN_CACHE["token"]

    if not CLIENT_ID or not CLIENT_SECRET:
        print("Missing EBAY_CLIENT_ID/SECRET")
        return None

    credentials = f"{CLIENT_ID}:{CLIENT_SECRET}"
    encoded_creds = base64.b64encode(credentials.encode()).decode()

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {encoded_creds}"
    }
    data = {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope"
    }

    print("Fetching eBay OAuth token...")
    try:
        auth_res = requests.post("https://api.ebay.com/identity/v1/oauth2/token", headers=headers, data=data, timeout=15)
    except Exception as e:
        print(f"Token request failed: {e}")
        return None
    
    if auth_res.status_code != 200:
        print(f"Failed to get token: {auth_res.text}")
        return None

    j = auth_res.json()
    token = j.get("access_token")
    expires_in = int(j.get("expires_in", 7200))
    TOKEN_CACHE["token"] = token
    TOKEN_CACHE["expiry"] = now + expires_in
    return token

def send_alerts(subject, body):
    """Sends alerts via configured channels (Email, Discord, Telegram)."""
    # Email
    if os.getenv("ENABLE_EMAIL", "false").lower() == "true":
        sender = os.getenv("EMAIL_SENDER")
        password = os.getenv("EMAIL_PASSWORD")
        receiver = os.getenv("EMAIL_RECEIVER")
        
        if password == "YOUR_GMAIL_APP_PASSWORD" or not password:
            print(f"\n[ALERT - WOULD EMAIL] {subject}")
            print("(Email not sent: Add your Gmail App Password to .env to enable email sending)")
        else:
            msg = MIMEText(body)
            msg['Subject'] = subject
            msg['From'] = sender
            msg['To'] = receiver
            try:
                with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
                    server.login(sender, password)
                    server.sendmail(sender, receiver, msg.as_string())
                print(f"Email sent successfully to {receiver}!")
            except Exception as e:
                print(f"Failed to send email: {e}")

    # Discord
    if os.getenv("ENABLE_DISCORD", "false").lower() == "true":
        bot_token = os.getenv("DISCORD_BOT_TOKEN")
        channel_id = os.getenv("DISCORD_CHANNEL_ID")
        if bot_token and channel_id:
            url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
            headers = {
                "Authorization": f"Bot {bot_token}",
                "Content-Type": "application/json"
            }
            # Truncate body if it exceeds discord's 2000 char limit
            content = f"**{subject}**\n\n{body}"
            if len(content) > 1950:
                content = content[:1950] + "... (truncated)"
            data = {"content": content}
            try:
                res = requests.post(url, headers=headers, json=data)
                if res.status_code in (200, 204):
                    print("Discord alert sent successfully!")
                else:
                    print(f"Failed to send Discord alert: {res.text}")
            except Exception as e:
                print(f"Failed to send Discord alert: {e}")
        else:
            print("Discord enabled but DISCORD_BOT_TOKEN or DISCORD_CHANNEL_ID is missing in .env")

    # Telegram - use MarkdownV2 with escaping or plain text fallback
    if os.getenv("ENABLE_TELEGRAM", "false").lower() == "true":
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if bot_token and chat_id:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            # Escape MarkdownV2 special chars or send plain text to avoid 400
            def escape_md(text):
                return re.sub(r'([_*\[\]()~`>#+\-=|{}.!])', r'\\\1', text)
            safe_body = escape_md(body[:3500])
            safe_subject = escape_md(subject)
            data = {
                "chat_id": chat_id,
                "text": f"*{safe_subject}*\n\n{safe_body}",
                "parse_mode": "MarkdownV2"
            }
            try:
                res = requests.post(url, json=data, timeout=10)
                if res.status_code == 200:
                    print("Telegram alert sent successfully!")
                else:
                    # fallback to plain text
                    res2 = requests.post(url, json={"chat_id": chat_id, "text": f"{subject}\n\n{body[:3500]}"}, timeout=10)
                    if res2.status_code == 200:
                        print("Telegram alert sent (plain text fallback)!")
                    else:
                        print(f"Failed to send Telegram alert: {res.text}")
            except Exception as e:
                print(f"Failed to send Telegram alert: {e}")
        else:
            print("Telegram enabled but TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing in .env")

def evaluate_deal_with_llm(listing_data):
    """Uses OpenCode (via OpenAI package) to evaluate the deal (multimodal). Handles 429/FreeUsageLimit."""
    
    # Truncate description to prevent token bloat
    description = listing_data.get('description', '')[:2500]
    
    prompt = f"""
    Evaluate this eBay listing:
    - Title: {listing_data['title']}
    - Total Price: ${listing_data['total_cost']:.2f}
    - Condition: {listing_data['condition']}
    - Seller Feedback Count: {listing_data['seller_reviews']}
    - Seller Positive Rating: {listing_data['seller_rating_pct']}%
    - Returns Accepted: {listing_data['returns_accepted']}
    
    Description Snippet:
    {description}
    
    Task:
    1. Is this listing the actual main item queried ({SEARCH_QUERY}), or is it just an accessory, replacement part, packaging/box, or manual? If it is an accessory or packaging, you MUST assign a FINAL_SCORE of 1.
    2. Does it match the specific model/version requested? Deduct points for mismatch.
    3. Is this price significantly below market average for the product described?
    4. Are there any physical defects visible in the images (stains, cracks, dents, heavy wear) that contradict the condition or make this a bad deal?
    5. Are there any hidden red flags in the title, description, or seller metrics?
    6. Give a final Deal Score from 1-10. Deduct points if the seller has low feedback (<50 reviews), if returns are not accepted, or if the images show undisclosed damage.
    {EXTRA_PROMPT}
    
    Please provide your reasoning and end with exactly: "FINAL_SCORE: X" where X is the number (1-10).
    """

    # Build multimodal message content
    content_blocks = [{"type": "text", "text": prompt}]
    
    # Add images if model supports vision (allowlist)
    if supports_vision(LLM_MODEL_NAME):
        for img_url in listing_data.get("images", []):
            content_blocks.append({
                "type": "image_url",
                "image_url": {"url": img_url}
            })

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model=LLM_MODEL_NAME,
                messages=[
                    {"role": "system", "content": "You are an expert deal finder and scam detector for eBay electronics."},
                    {"role": "user", "content": content_blocks}
                ],
                temperature=0.3
            )
            content = response.choices[0].message.content
            print(f"\n--- LLM Evaluation ---\n{content}\n----------------------")
            
            # Extract the score
            for line in content.split('\n'):
                if "FINAL_SCORE:" in line:
                    try:
                        score = int(line.split("FINAL_SCORE:")[1].strip())
                        return score, content
                    except ValueError:
                        pass
            return 0, content
        except Exception as e:
            err_str = str(e)
            # Detect rate-limit / free-tier quota - do NOT return 0 (false negative)
            is_rate_limit = "429" in err_str or "Rate limit" in err_str or "FreeUsageLimit" in err_str or "rate_limit" in err_str.lower()
            if is_rate_limit:
                if attempt < max_retries - 1:
                    backoff = 5 * (2 ** attempt)  # 5s, 10s, 20s
                    print(f"LLM rate-limited (attempt {attempt+1}/{max_retries}): {e} - retrying in {backoff}s...")
                    time.sleep(backoff)
                    continue
                print(f"LLM rate-limited after {max_retries} retries: {e}")
                print(" -> Free tier quota hit. Pausing LLM evals this cycle. Check https://opencode.ai dashboard or wait for reset.")
                return None, f"RATE_LIMITED: {err_str}"
            print(f"LLM Evaluation failed: {e}")
            return 0, err_str
    return None, "RATE_LIMITED: max retries exceeded"

def get_best_category(token, query):
    """Uses eBay's Taxonomy API to find the best category ID and name for the query."""
    url = f"https://api.ebay.com/commerce/taxonomy/v1/category_tree/0/get_category_suggestions?q={requests.utils.quote(query)}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json"
    }
    try:
        res = requests.get(url, headers=headers)
        if res.status_code == 200:
            suggestions = res.json().get("categorySuggestions", [])
            if suggestions:
                best_cat = suggestions[0].get("category", {})
                return best_cat.get("categoryId"), best_cat.get("categoryName")
    except Exception as e:
        print(f"Failed to fetch category suggestions: {e}")
    return None, None

def generate_negative_keywords(query, category_name):
    """Uses LLM to dynamically generate negative keywords for the specific product."""
    prompt = f"""
    The user is searching eBay for: "{query}".
    The detected category is: "{category_name}".

    Please provide a list of negative keywords to exclude accessories, parts, packaging, and unrelated junk.
    For example:
    - For shoes: ["box", "laces", "sole", "empty"]
    - For laptops: ["case", "charger", "cover", "box", "sleeve", "parts", "battery", "screen"]
    - For phones: ["case", "screen protector", "box", "parts", "empty"]

    Return ONLY a valid JSON array of strings, nothing else. Example: ["case", "charger"]
    """
    try:
        response = client.chat.completions.create(
            model=LLM_MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1
        )
        content = response.choices[0].message.content.strip()
        # Clean up any markdown blocks if the LLM output them
        if content.startswith("```json"):
            content = content[7:-3].strip()
        elif content.startswith("```"):
            content = content[3:-3].strip()
        
        return json.loads(content)
    except Exception as e:
        print(f"Failed to generate dynamic filters: {e}")
        return []

def get_item_details(token, item_id):
    """Fetches full description and image URLs for an item."""
    url = f"https://api.ebay.com/buy/browse/v1/item/{requests.utils.quote(item_id)}"
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        "Accept": "application/json"
    }
    try:
        res = requests.get(url, headers=headers)
        if res.status_code == 200:
            data = res.json()
            
            # Combine condition description and main description
            cond_desc = data.get("conditionDescription", "")
            main_desc = data.get("description", "")
            full_desc = f"{cond_desc}\n{main_desc}"
            
            # Strip HTML tags
            clean_desc = re.sub('<[^<]+>', '', full_desc).strip()
            
            # Extract images (up to 4 total to save tokens)
            images = []
            if data.get("image", {}).get("imageUrl"):
                images.append(data["image"]["imageUrl"])
            
            for img in data.get("additionalImages", [])[:3]:
                if img.get("imageUrl"):
                    images.append(img["imageUrl"])
                    
            return clean_desc, images
    except Exception as e:
        print(f"Failed to fetch item details: {e}")
        
    return "", []

def search_ebay_deals(token, effective_category_id=None):
    """Searches eBay for new listings matching our criteria. Returns count of new alerts."""
    search_headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        "Content-Type": "application/json"
    }

    # Expanded conditions filter; removed topRatedSellingProgram requirement
    filters = "buyingOptions:{FIXED_PRICE},conditions:{USED|NEW|REFURBISHED}"
    if MAX_PRICE:
        filters += f",price:[0..{MAX_PRICE}],priceCurrency:USD"
    
    # Send clean query to eBay to avoid stripping valid bundle listings
    full_query = SEARCH_QUERY.strip()

    params = {
        "q": full_query,
        "sort": "newlyListed",
        "filter": filters,
        "limit": "20"
    }

    if effective_category_id:
        params["category_ids"] = effective_category_id

    print(f"Searching eBay Live API for '{SEARCH_QUERY}'...")
    if effective_category_id:
        print(f"Restricted to Category: {effective_category_id}")
        
    try:
        res = requests.get("https://api.ebay.com/buy/browse/v1/item_summary/search", headers=search_headers, params=params, timeout=15)
    except Exception as e:
        print(f"Browse API request failed: {e}")
        return 0
    
    if res.status_code == 429:
        print("Browse API throttled (429), backing off 60s")
        time.sleep(60)
        return 0
    if res.status_code != 200:
        print(f"Error calling Browse API: {res.text}")
        return 0

    items = res.json().get("itemSummaries", [])
    if not items:
        print("No new items found.")
        return 0

    new_alerts = 0
    for idx, item in enumerate(items):
        title = item.get("title", "")
        item_id = item.get("itemId")
        if not item_id:
            continue
        # Dedup: skip seen items (only mark seen after successful eval)
        if item_id in SEEN_ITEMS:
            continue
        
        # Extract Price & Shipping
        price = float(item.get("price", {}).get("value", 0))
        shipping_options = item.get("shippingOptions", [{}])
        shipping = 0.0
        if shipping_options and isinstance(shipping_options[0], dict):
            shipping = float(shipping_options[0].get("shippingCost", {}).get("value", 0))
        total_cost = price + shipping

        # Extract Seller Ratings
        seller_info = item.get("seller", {})
        feedback_score = int(seller_info.get("feedbackScore", 0))
        feedback_pct = float(seller_info.get("feedbackPercentage", "0.0"))
        
        # Condition
        condition_id = item.get("conditionId", "")
        condition = item.get("condition", "Unknown")
        
        returns_accepted = item.get("returnsAccepted", False)
        
        # --- HARD FILTERS ---
        
        # Skip For Parts (7000)
        if str(condition_id) == "7000":
            SEEN_ITEMS.add(item_id)
            continue
            
        # Hard Trust Filters - relaxed: was <10/<95, now <5/<90 to keep small sellers
        if feedback_score < 5 or feedback_pct < 90.0:
            SEEN_ITEMS.add(item_id)
            continue
            
        # Description / Title Traps
        lower_title = title.lower()
        traps = ["box only", "as-is", "untested", "for parts", "read description"]
        if any(trap in lower_title for trap in traps):
            SEEN_ITEMS.add(item_id)
            continue
            
        # Compile data for LLM (Basic)
        listing_data = {
            "title": title,
            "total_cost": total_cost,
            "condition": condition,
            "seller_reviews": feedback_score,
            "seller_rating_pct": feedback_pct,
            "returns_accepted": returns_accepted,
            "url": item.get("itemWebUrl")
        }
        
        # Fetch deep details (Description + Images) since it passed hard filters
        print(f"Fetching full description & images for: {title}")
        desc, images = get_item_details(token, item_id)
        listing_data["description"] = desc
        listing_data["images"] = images
        
        print(f"Evaluating: {title} | Total: ${total_cost:.2f}")
        score, explanation = evaluate_deal_with_llm(listing_data)

        # Handle free-tier rate limit: don't mark seen, pause evals
        if score is None:
            print(f"Rate limit hit during eval for {item_id} - pausing remaining {len(items)-idx-1} items this cycle. Will retry next poll.")
            # do NOT add to SEEN_ITEMS, so it retries next cycle
            break

        # Mark seen only after successful LLM eval
        SEEN_ITEMS.add(item_id)
        
        if score >= 8:
            print(f"🔥 HOT DEAL ALERT! Score: {score}/10")
            subject = f"eBay Deal Alert ({score}/10): {title}"
            body = f"Found a deal for '{SEARCH_QUERY}'!\n\n"
            body += f"Title: {title}\n"
            body += f"Total Cost: ${total_cost:.2f}\n"
            body += f"URL: {listing_data['url']}\n\n"
            body += f"--- Evaluation ---\n{explanation}"
            
            send_alerts(subject, body)
            new_alerts += 1
        else:
            print(f"Deal score too low ({score}/10). Skipping alert.")

        # Throttle free tier: 2s gap between LLM calls
        if idx < len(items) - 1:
            time.sleep(2)

    save_seen()
    print(f"Checked {len(items)} items, {new_alerts} new alerts. Seen total: {len(SEEN_ITEMS)}")
    return new_alerts

def main():
    validate_env()
    load_seen()
    print(f"Starting eBay Deal Finder Daemon for '{SEARCH_QUERY}'...")
    if MAX_PRICE:
        print(f"Max Price Filter: ${MAX_PRICE}")
    if CATEGORY_ID:
        print(f"Using fixed Category: {CATEGORY_ID}")
        
    while True:
        token = get_ebay_token()
        if token:
            # Determine effective category without mutating global
            effective_category_id = CATEGORY_ID
            if not effective_category_id:
                best_id, category_name = get_best_category(token, SEARCH_QUERY)
                if best_id:
                    print(f"Dynamic Category Detected: {category_name} (ID: {best_id})")
                    effective_category_id = best_id
                else:
                    print("Could not dynamically determine category, searching without category filter.")
            
            # Search - clean query, no negative-keyword over-filtering
            # LLM filtering happens in evaluate_deal_with_llm step
            search_ebay_deals(token, effective_category_id=effective_category_id)
        else:
            print("Skipping search due to token failure, will retry next poll.")
            
        if RUN_ONCE:
            break
            
        print(f"Sleeping for {POLL_INTERVAL_SECONDS} seconds...")
        time.sleep(POLL_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
