import os
import sys
import time
import base64
import requests
import smtplib
from email.mime.text import MIMEText
from dotenv import load_dotenv
import openai

import argparse

# Load environment variables
load_dotenv()

CLIENT_ID = os.getenv("EBAY_CLIENT_ID")
CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Set up OpenAI client (OpenCode API key should work seamlessly with the OpenAI package)
# Using OpenCode's Zen gateway API URL
client = openai.OpenAI(
    api_key=OPENAI_API_KEY,
    base_url="https://opencode.ai/zen/v1"
)

# Parse command line arguments
parser = argparse.ArgumentParser(description="eBay Deal Finder Daemon")
parser.add_argument("query", nargs='?', default="MacBook Pro M3", help="Search query")
parser.add_argument("--max-price", type=float, help="Maximum price (USD)")
parser.add_argument("--extra-prompt", type=str, default="", help="Extra instructions for the LLM")
parser.add_argument("--once", action="store_true", help="Run only once and exit")
args = parser.parse_args()

SEARCH_QUERY = args.query
MAX_PRICE = args.max_price
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

def send_email_alert(subject, body):
    """Sends an email alert for a found deal."""
    sender = os.getenv("EMAIL_SENDER")
    password = os.getenv("EMAIL_PASSWORD")
    receiver = os.getenv("EMAIL_RECEIVER")
    
    if password == "YOUR_GMAIL_APP_PASSWORD" or not password:
        print(f"\n[ALERT - WOULD EMAIL] {subject}")
        print(body)
        print("(Email not sent: Add your Gmail App Password to .env to enable email sending)\n")
        return
        
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

def evaluate_deal_with_llm(listing_data):
    """Uses OpenCode (via OpenAI package) to evaluate the deal and return a score."""
    prompt = f"""
    Evaluate this eBay listing:
    - Title: {listing_data['title']}
    - Total Price: ${listing_data['total_cost']:.2f}
    - Condition: {listing_data['condition']}
    - Seller Feedback Count: {listing_data['seller_reviews']}
    - Seller Positive Rating: {listing_data['seller_rating_pct']}%
    - Returns Accepted: {listing_data['returns_accepted']}
    
    Task:
    1. Is this listing the actual main item queried ({SEARCH_QUERY}), or is it just an accessory, replacement part, packaging/box, or manual? If it is an accessory or packaging (e.g., a case, bag, box, charger, or strap instead of the product itself), you MUST assign a FINAL_SCORE of 1.
    2. Does it match the specific model/version requested in "{SEARCH_QUERY}"? If it is a different model/spec (e.g., an Intel model when they searched for M1, or a different brand), evaluate it normally as a deal but deduct points for not matching the target specifications.
    3. Is this price significantly below market average for the product described?
    4. Is the seller trustworthy enough to warrant buying this deal?
    5. Are there any hidden red flags based on the title or metrics?
    6. Give a final Deal Score from 1-10. Deduct points if the seller has low feedback (<50 reviews) even if their percentage is 100%, or if they do not accept returns.
    {EXTRA_PROMPT}
    
    Please provide your reasoning and end with exactly: "FINAL_SCORE: X" where X is the number (1-10).
    """

    try:
        response = client.chat.completions.create(
            model="deepseek-v4-flash-free",
            messages=[
                {"role": "system", "content": "You are an expert deal finder and scam detector for eBay electronics."},
                {"role": "user", "content": prompt}
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

def search_ebay_deals(token):
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
    
    params = {
        "q": SEARCH_QUERY,
        "sort": "newlyListed",
        "filter": filters,
        "limit": "20"
    }

    print(f"Searching eBay Sandbox for '{SEARCH_QUERY}'...")
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
            
        # Compile data for LLM
        listing_data = {
            "title": title,
            "total_cost": total_cost,
            "condition": condition,
            "seller_reviews": feedback_score,
            "seller_rating_pct": feedback_pct,
            "returns_accepted": returns_accepted,
            "url": item.get("itemWebUrl")
        }
        
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
            
            send_email_alert(subject, body)
        else:
            print(f"Deal score too low ({score}/10). Skipping alert.")

def main():
    print(f"Starting eBay Deal Finder Daemon for '{SEARCH_QUERY}'...")
    if MAX_PRICE:
        print(f"Max Price Filter: ${MAX_PRICE}")
        
    while True:
        token = get_ebay_token()
        if token:
            search_ebay_deals(token)
            
        if RUN_ONCE:
            break
        
        print(f"Sleeping for {POLL_INTERVAL_SECONDS} seconds...")
        time.sleep(POLL_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
