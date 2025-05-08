from flask import Flask, render_template, request, jsonify
from googlesearch import search
import requests
import logging
from flask_cors import CORS
import json
from datetime import datetime
import re
import asyncio
import nest_asyncio
from playwright.async_api import async_playwright
import os

# Apply nest_asyncio for Playwright in Flask's single-threaded environment
nest_asyncio.apply()

app = Flask(__name__)
CORS(app)  # Allow cross-origin requests
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Cache for googlesearch library results
search_cache = {}

# --- Streamlit Code Integration ---
def query_ollama(prompt):
    """Query the local Ollama model to process the prompt."""
    url = "http://localhost:11434/api/generate"
    payload = {
        "model": "llama3.2",
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.5,
            "top_p": 0.9,
            "max_tokens": 200
        }
    }
    try:
        response = requests.post(url, json=payload)
        if response.status_code == 200:
            return response.json()["response"]
        else:
            raise Exception(f"Ollama API error: {response.status_code} - {response.text}")
    except Exception as e:
        return f"Error querying Ollama: {str(e)}"

def extract_email_details(task):
    """Extract email, topic, and body from the task using Ollama, with fallbacks."""
    prompt = (
        f"Extract the email address, message topic, and body text from this task. "
        f"Return only the extracted values in this exact format: 'email: <email>, topic: <topic>, body: <body>'. "
        f"If any part is missing, use 'None' for that field, except for the body: if no explicit body is provided, "
        f"generate a short, relevant body based on the topic. Do not include extra text beyond the format.\n"
        f"Task: {task}"
    )
    result = query_ollama(prompt)
    
    email_match = re.search(r"email: ([^,]+),", result)
    topic_match = re.search(r"topic: ([^,]+),", result)
    body_match = re.search(r"body: (.+)", result)
    email = email_match.group(1).strip() if email_match else None
    topic = topic_match.group(1).strip() if topic_match else None
    body = body_match.group(1).strip() if body_match else None

    if not email or email == "None":
        email_fallback = re.search(r"['\"]([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})['\"]", task)
        email = email_fallback.group(1) if email_fallback else None
    if not topic or topic == "None":
        topic_fallback = re.search(r"about ['\"]([^'\"]+)['\"]", task, re.IGNORECASE)
        topic = topic_fallback.group(1) if topic_fallback else None
    if not body or body == "None":
        if topic and topic != "None":
            body = f"Hi, I’d like to discuss the {topic}. Are you available?"
        else:
            body_fallback = re.search(r"body\s*['\"]([^'\"]+)['\"]", task, re.IGNORECASE)
            body = body_fallback.group(1) if body_fallback else None

    return {"email": email, "topic": topic, "body": body}

async def send_gmail(page, email, topic, body):
    """Automate Gmail to send an email using Playwright."""
    output = []
    compose_url = "https://mail.google.com/mail/u/0/#inbox?compose=new"
    output.append("📧 Opening Gmail compose window...")

    await page.goto(compose_url, wait_until='load', timeout=90000)

    try:
        consent_dialog = page.locator('text="I agree"')
        if await consent_dialog.count():
            await consent_dialog.click()
            output.append("✅ Consent dialog accepted")
    except Exception:
        pass

    try:
        TO_SELECTOR = 'input[aria-label="To recipients"]'
        await page.wait_for_selector(TO_SELECTOR, state="visible", timeout=30000)
        output.append("✅ To field is visible, compose window ready")
    except Exception as e:
        output.append(f"❌ Failed to find visible To field: {str(e)}")
        return output

    TO_SELECTOR = 'input[aria-label="To recipients"]'
    SUBJECT_SELECTOR = 'input[aria-label="Subject"]'
    BODY_SELECTOR = 'div[role="textbox"][aria-label="Message Body"]'
    SEND_BUTTON = '[role="button"][data-tooltip*="Send"]'

    async def robust_fill(selector, value, field_name):
        try:
            field = page.locator(selector)
            await field.wait_for(state="visible", timeout=15000)
            await field.scroll_into_view_if_needed()
            await field.click()
            await field.fill('')
            await field.type(value, delay=100)
            await asyncio.sleep(1)
            if "input" in selector:
                current_value = await field.input_value()
                if value in current_value:
                    output.append(f"✅ {field_name} filled successfully")
                    return True
            else:
                current_value = await field.inner_text()
                if value in current_value:
                    output.append(f"✅ {field_name} filled successfully")
                    return True
            return False
        except Exception as e:
            output.append(f"❌ {field_name} error: {str(e)}")
            return False

    if email and email != "None":
        if await robust_fill(TO_SELECTOR, email, "To field"):
            await page.keyboard.press("Tab")
        else:
            output.append("🔄 Alternative To field approach")
            await page.keyboard.type(email, delay=100)
            await page.keyboard.press("Tab")

    if topic and topic != "None":
        if await robust_fill(SUBJECT_SELECTOR, topic, "Subject"):
            await page.keyboard.press("Tab")

    if body and body != "None":
        try:
            body_field = page.locator(BODY_SELECTOR)
            await body_field.wait_for(state="visible", timeout=15000)
            await body_field.click()
            await body_field.fill('')
            await page.keyboard.type(body.strip('"'), delay=100)
            output.append("✅ Body field filled successfully")
        except Exception as e:
            output.append(f"❌ Body field error: {str(e)}")

    try:
        send_button = page.locator(SEND_BUTTON)
        await send_button.wait_for(state="visible", timeout=10000)
        await send_button.click()
        output.append("✅ Send button clicked")
        await asyncio.sleep(5)
        if "message sent" in (await page.content()).lower():
            output.append("🎉 Email successfully sent")
        else:
            output.append("❌ Send verification failed")
    except Exception as e:
        output.append(f"❌ Send error: {str(e)}. Using keyboard shortcut")
        await page.keyboard.down("Control")
        await page.keyboard.press("Enter")
        await page.keyboard.up("Control")

    return output

async def google_search(page, query):
    """Perform a Google search and extract top results using Playwright."""
    output = []
    google_url = "https://www.google.com"
    output.append(f"🔍 Performing Google search for: {query}")

    await page.goto(google_url, wait_until='load', timeout=90000)

    try:
        search_bar = page.locator('textarea[name="q"]')
        await search_bar.wait_for(state="visible", timeout=30000)
    except Exception as e:
        output.append(f"❌ Failed to find search bar: {str(e)}")
        return output, []

    await search_bar.click()
    await search_bar.type(query, delay=100)
    await page.keyboard.press("Enter")
    await page.wait_for_load_state('load', timeout=90000)

    results = []
    try:
        search_results = page.locator('div#search div.g')
        count = await search_results.count()
        for i in range(min(5, count)):
            result = search_results.nth(i)
            title_elem = result.locator('h3')
            link_elem = result.locator('a')
            title = await title_elem.text_content() if await title_elem.count() else "No title"
            url = await link_elem.get_attribute('href') if await link_elem.count() else "No URL"
            results.append({"title": title.strip(), "url": url, "description": "No description"})
        output.append("✅ Search results extracted successfully")
    except Exception as e:
        output.append(f"❌ Error extracting search results: {str(e)}")

    output.append(json.dumps(results, indent=2))
    return output, results

async def youtube_search_and_play(page, query):
    """Navigate to YouTube, search for a video, and play the first result."""
    output = []
    youtube_url = "https://www.youtube.com"
    output.append(f"🎥 Navigating to YouTube and searching for: {query}")

    await page.goto(youtube_url, wait_until='load', timeout=90000)

    try:
        await page.wait_for_selector('yt-icon#logo-icon', state="visible", timeout=60000)
        output.append("✅ YouTube page is interactive")
    except Exception as e:
        output.append(f"❌ Failed to confirm page interactivity: {str(e)}")
        return output

    try:
        consent_button = page.locator('button:visible', text=re.compile(r"(Accept|I agree)", re.I))
        if await consent_button.count():
            await consent_button.first.click()
            output.append("✅ Consent screen accepted")
            await asyncio.sleep(3)
    except Exception as e:
        output.append(f"ℹ️ No consent screen or error handling it: {str(e)}")

    try:
        search_bar = page.locator('ytd-searchbox input#search')
        await search_bar.wait_for(state="visible", timeout=60000)
        output.append("✅ YouTube search bar found")
    except Exception as e:
        output.append(f"❌ Failed to find YouTube search bar: {str(e)}")
        return output

    await search_bar.click()
    await search_bar.type(query, delay=100)
    await page.keyboard.press("Enter")
    await page.wait_for_load_state('load', timeout=90000)

    try:
        first_video = page.locator('ytd-video-renderer #video-title').first
        await first_video.wait_for(state="visible", timeout=30000)
        video_title = await first_video.text_content()
        output.append(f"✅ Selecting first video: {video_title.strip()}")
        await first_video.click()
        await asyncio.sleep(5)
        output.append("🎬 Video is now playing")
    except Exception as e:
        output.append(f"❌ Error selecting first video: {str(e)}")

    return output

# --- Existing Functionality ---
def is_real_time_query(message):
    real_time_keywords = ['latest', 'current', 'today', 'now', 'real-time', 'recent', 'breaking', 'live', 'this week', 'this month']
    return any(keyword in message.lower() for keyword in real_time_keywords)

def fetch_search_results(query):
    """Use googlesearch library for non-@googlesearch real-time queries."""
    logger.info(f"Fetching search results for: {query}")
    if query in search_cache:
        logger.info("Returning cached results")
        return search_cache[query]
    
    try:
        results = []
        for result in search(
            query,
            num_results=3,
            advanced=True,
            sleep_interval=2
        ):
            results.append({
                'title': result.title or 'No title',
                'description': result.description or 'No description available',
                'url': result.url or 'No URL'
            })
        search_cache[query] = results
        logger.info(f"Found {len(results)} results")
        return results
    except Exception as e:
        logger.error(f"Search error: {str(e)}")
        return []

async def process_special_command(message):
    """Handle @googlesearch, @mail, @youtube commands using Playwright."""
    chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    user_data_dir = os.path.expanduser("~/ChromeProfile")
    headless_mode = False

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            executable_path=chrome_path,
            headless=headless_mode,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--start-maximized"
            ],
            viewport={"width": 1366, "height": 768},
            ignore_https_errors=True
        )
        page = await context.new_page()
        await page.add_init_script("""
            delete Object.getPrototypeOf(navigator).webdriver;
            window.chrome = { runtime: {} };
        """)

        output = []
        sources = []
        task_section = "Conversation"

        if message.lower().startswith("@googlesearch"):
            query = message[len("@googlesearch"):].strip()
            if not query:
                output.append("🚫 Error: No search query provided after @googlesearch")
            else:
                task_section = "Search (@googlesearch)"
                search_output, search_results = await google_search(page, query)
                output.extend(search_output)
                sources = search_results
                output.append("✅ Search completed")
        
        elif message.lower().startswith("@mail"):
            email_task = message[len("@mail"):].strip()
            if not email_task:
                output.append("🚫 Error: No email details provided after @mail")
            else:
                task_section = "Email (@mail)"
                details = extract_email_details(email_task)
                output.append(f"📋 Extracted Details: {json.dumps(details, indent=2)}")
                mail_output = await send_gmail(page, details["email"], details["topic"], details["body"])
                output.extend(mail_output)
                output.append("✅ Email process completed")
        
        elif message.lower().startswith("@youtube"):
            query = message[len("@youtube"):].strip()
            if not query:
                output.append("🚫 Error: No YouTube query provided after @youtube")
            else:
                task_section = "YouTube (@youtube)"
                youtube_output = await youtube_search_and_play(page, query)
                output.extend(youtube_output)
                output.append("✅ YouTube process completed")
        
        await asyncio.sleep(5)
        await context.close()
        return output, sources, task_section

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/chat', methods=['POST'])
async def chat():
    try:
        data = request.get_json()
        message = data.get('message')
        if not message:
            return jsonify({'error': 'Message is required'}), 400

        logger.info(f"Processing query: {message}")

        # Handle special commands (@googlesearch, @mail, @youtube)
        if message.lower().startswith(("@googlesearch", "@mail", "@youtube")):
            output, sources, task_section = await process_special_command(message)
            return jsonify({
                'message': "\n".join(output),
                'sources': sources,
                'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                'task_section': task_section
            })

        # Handle regular chat or real-time queries
        is_real_time = is_real_time_query(message)
        logger.info(f"Real-time: {is_real_time}")

        system_prompt = """
            You are ANS, an advanced AI chatbot developed by Ajay, Shalini, and Sebastian. Your system architecture integrates Llama 3.2 with a Progressive Neural Network (PNN), enabling you to provide insightful and adaptive responses. You are designed to be helpful, friendly, and accurate, offering assistance across a wide range of topics. Respond to the user's query in a conversational and engaging manner, ensuring clarity and relevance. If the query is unclear, ask for clarification politely. Always strive to provide useful and truthful information, and if uncertain, acknowledge limitations humbly. For queries requiring real-time data, use the provided search results to inform your response. If no search results are available, rely on your knowledge and inform the user that real-time data could not be fetched.
        """

        prompt = system_prompt + "\n\n"
        sources = []
        task_section = "Conversation"

        if is_real_time:
            search_results = fetch_search_results(message)
            if search_results:
                prompt += "Search Results:\n" + "\n\n".join(
                    f"{i+1}. {r['title']}\n{r['description']}\nSource: {r['url']}"
                    for i, r in enumerate(search_results)
                ) + "\n\n"
                sources = search_results
            else:
                prompt += "No relevant search results found. Please answer based on your knowledge and note that real-time data could not be fetched.\n\n"

        prompt += f"User: {message}"

        logger.info("Sending prompt to Ollama")
        ollama_response = requests.post(
            'http://localhost:11434/api/generate',
            json={
                'model': 'llama3.2',
                'prompt': prompt,
                'stream': False
            }
        )
        if ollama_response.status_code != 200:
            logger.error(f"Ollama error: {ollama_response.text}")
            return jsonify({'error': 'Failed to process query'}), 500

        response_data = ollama_response.json()
        response = response_data.get('response', 'No response received.')
        logger.info("Ollama response received")

        return jsonify({
            'message': response,
            'sources': sources,
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'task_section': task_section
        })

    except Exception as e:
        logger.error(f"Chat error: {str(e)}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)
