import os
import sys
import time
import base64
import requests
import smtplib
from email.mime.text import MIMEText
from dotenv import load_dotenv
import openai
import json
import re
import argparse

# Load environment variables
load_dotenv()

CLIENT_ID = os.getenv("EBAY_CLIENT_ID")
CLIENT_ID = os.getenv("EBAY_CLIENT_ID")
CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
LLM_MODEL_NAME = os.getenv("LLM_MODEL_NAME", "gemini-3.5-flash")

# Set up OpenAI client (OpenCode API key should work seamlessly with the OpenAI package)
# Using OpenCode's Go gateway API URL
client = openai.OpenAI(
    api_key=OPENAI_API_KEY,
    base_url="https://opencode.ai/zen/go/v1"
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

def get_ebay_token():
    """Gets an OAuth token from the eBay Sandbox."""
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
    auth_res = requests.post("https://api.ebay.com/identity/v1/oauth2/token", headers=headers, data=data)
    
    if auth_res.status_code != 200:
        print(f"Failed to get token: {auth_res.text}")
        return None
        
    return auth_res.json().get("access_token")

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

    # Telegram
    if os.getenv("ENABLE_TELEGRAM", "false").lower() == "true":
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if bot_token and chat_id:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            data = {
                "chat_id": chat_id,
                "text": f"*{subject}*\n\n{body}",
                "parse_mode": "Markdown"
            }
            try:
                res = requests.post(url, json=data)
                if res.status_code == 200:
                    print("Telegram alert sent successfully!")
                else:
                    print(f"Failed to send Telegram alert: {res.text}")
            except Exception as e:
                print(f"Failed to send Telegram alert: {e}")
        else:
            print("Telegram enabled but TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing in .env")

def evaluate_deal_with_llm(listing_data):
    """Uses OpenCode (via OpenAI package) to evaluate the deal (multimodal)."""
    
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
    
    # Add images if available AND model supports vision (DeepSeek does not)
    if "deepseek" not in LLM_MODEL_NAME.lower():
        for img_url in listing_data.get("images", []):
            content_blocks.append({
                "type": "image_url",
                "image_url": {"url": img_url}
            })

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
        print(f"LLM Evaluation failed: {e}")
        return 0, str(e)

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

def search_ebay_deals(token, exclusions=None):
    """Searches eBay for new listings matching our criteria."""
    search_headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        "Content-Type": "application/json"
    }

    # Combined filters: Buy It Now, Used/New conditions, Top Rated Sellers only
    filters = "buyingOptions:{FIXED_PRICE},conditions:{USED|NEW},topRatedSellingProgram:true"
    if MAX_PRICE:
        filters += f",price:[0..{MAX_PRICE}],priceCurrency:USD"
    
    # Use provided exclusions or default empty
    exclusions = exclusions or []
    exclude_str = " ".join([f"-{word}" for word in exclusions])
    full_query = f"{SEARCH_QUERY} {exclude_str}".strip()

    params = {
        "q": full_query,
        "sort": "newlyListed",
        "filter": filters,
        "limit": "20"
    }

    if CATEGORY_ID:
        params["category_ids"] = CATEGORY_ID

    print(f"Searching eBay Live API for '{SEARCH_QUERY}' (excl. accessories)...")
    if CATEGORY_ID:
        print(f"Restricted to Category: {CATEGORY_ID}")
        
    res = requests.get("https://api.ebay.com/buy/browse/v1/item_summary/search", headers=search_headers, params=params)
    
    if res.status_code != 200:
        print(f"Error calling Browse API: {res.text}")
        return

    items = res.json().get("itemSummaries", [])
    if not items:
        print("No new items found.")
        return

    for item in items:
        title = item.get("title", "")
        item_id = item.get("itemId")
        
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
            continue
            
        # Hard Trust Filters
        if feedback_score < 10 or feedback_pct < 95.0:
            continue
            
        # Description / Title Traps
        lower_title = title.lower()
        traps = ["box only", "as-is", "untested", "for parts", "read description"]
        if any(trap in lower_title for trap in traps):
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
        
        if score >= 8:
            print(f"🔥 HOT DEAL ALERT! Score: {score}/10")
            subject = f"eBay Deal Alert ({score}/10): {title}"
            body = f"Found a deal for '{SEARCH_QUERY}'!\n\n"
            body += f"Title: {title}\n"
            body += f"Total Cost: ${total_cost:.2f}\n"
            body += f"URL: {listing_data['url']}\n\n"
            body += f"--- Evaluation ---\n{explanation}"
            
            send_alerts(subject, body)
        else:
            print(f"Deal score too low ({score}/10). Skipping alert.")

def main():
    print(f"Starting eBay Deal Finder Daemon for '{SEARCH_QUERY}'...")
    if MAX_PRICE:
        print(f"Max Price Filter: ${MAX_PRICE}")
        
    global CATEGORY_ID
    while True:
        token = get_ebay_token()
        if token:
            # 1. Determine dynamic category if not provided
            category_name = "Unknown"
            if not CATEGORY_ID:
                best_id, category_name = get_best_category(token, SEARCH_QUERY)
                if best_id:
                    print(f"Dynamic Category Detected: {category_name} (ID: {best_id})")
                    CATEGORY_ID = best_id
                else:
                    print("Could not dynamically determine category.")
            
            # 2. Generate dynamic negative keywords
            print("Generating dynamic negative keywords...")
            dynamic_exclusions = generate_negative_keywords(SEARCH_QUERY, category_name)
            print(f"Applying Exclusions: {dynamic_exclusions}")
            
            # 3. Search
            search_ebay_deals(token, exclusions=dynamic_exclusions)
            
        if RUN_ONCE:
            break
            
        print(f"Sleeping for {POLL_INTERVAL_SECONDS} seconds...")
        time.sleep(POLL_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
