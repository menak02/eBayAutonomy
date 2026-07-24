# eBay Autonomy: AI Deal Finder Daemon

A Python background daemon that queries the eBay Browse API for new listings, applies hard filtering thresholds (trust scores, rating history, condition, and search-hijacking protection), and uses OpenCode LLMs (OpenAI-compatible) to evaluate deal quality.

## Features
- **Adaptive LLM Evaluation:** Evaluates listings dynamically to spot scams, mislabeled items, and bait-and-switch attempts. Accessory items (like cases or packaging) are automatically rejected.
- **Custom Search Logic:** Pass target searches, max prices, and specialized evaluation prompts via command-line arguments.
- **Continuous Polling:** Runs persistently in the background, checking for new listings every 5 minutes.
- **Email Alerts:** Automatically emails you a summary when a deal receives a score of 8/10 or higher.

## Setup
1. Clone the repository and navigate into it:
   ```bash
   cd eBayAutonomy
   ```
2. Create and activate a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Create a `.env` file in the root directory:
   ```env
   EBAY_CLIENT_ID=your_production_app_id
   EBAY_CLIENT_SECRET=your_production_cert_id
   OPENAI_API_KEY=your_opencode_api_key

   EMAIL_SENDER=your_gmail_address
   EMAIL_PASSWORD=your_gmail_app_password
   EMAIL_RECEIVER=your_receiver_address
   ```

## Usage
Run the script with your search query, price thresholds, and optional LLM context instructions:
```bash
python ebay_deal_finder.py "M1 MacBook" --max-price 300 --extra-prompt "Deduct points significantly if battery health is poor or cycle counts are high."
```
To run it once and exit instead of loop:
```bash
python ebay_deal_finder.py "M1 MacBook" --max-price 300 --once
```
