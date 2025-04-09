import asyncio
from playwright.async_api import async_playwright
import requests
import re
import os
from datetime import datetime

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
    response = requests.post(url, json=payload)
    if response.status_code == 200:
        return response.json()["response"]
    else:
        raise Exception(f"Ollama API error: {response.status_code} - {response.text}")

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
    compose_url = "https://mail.google.com/mail/u/0/#inbox?compose=new"
    print(f"Opening Gmail compose window...")

    await page.goto(compose_url, wait_until='load', timeout=90000)

    try:
        consent_dialog = page.locator('text="I agree"')
        if await consent_dialog.count():
            await consent_dialog.click()
    except Exception:
        pass

    try:
        TO_SELECTOR = 'input[aria-label="To recipients"]'
        await page.wait_for_selector(TO_SELECTOR, state="visible", timeout=30000)
        print("To field is visible, compose window ready")
    except Exception as e:
        print(f"Failed to find visible To field: {str(e)}")
        await page.screenshot(path="to_field_failure.png")
        return

    TO_SELECTOR = 'input[aria-label="To recipients"]'
    SUBJECT_SELECTOR = 'input[aria-label="Subject"]'
    BODY_SELECTOR = 'div[role="textbox"][aria-label="Message Body"]'
    SEND_BUTTON = '[role="button"][data-tooltip*="Send"]'

    async def robust_fill(selector, value, field_name):
        """Robustly fill a field and verify the input."""
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
                    print(f"{field_name} filled successfully")
                    return True
            else:
                current_value = await field.inner_text()
                if value in current_value:
                    print(f"{field_name} filled successfully")
                    return True
            return False
        except Exception as e:
            print(f"{field_name} error: {str(e)}")
            return False

    if email and email != "None":
        if await robust_fill(TO_SELECTOR, email, "To field"):
            await page.keyboard.press("Tab")
        else:
            print("Alternative To field approach")
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
            print("Body field filled successfully")
        except Exception as e:
            print(f"Body field error: {str(e)}")

    try:
        send_button = page.locator(SEND_BUTTON)
        await send_button.wait_for(state="visible", timeout=10000)
        await send_button.click()
        print("Send button clicked")
        await asyncio.sleep(5)
        if "message sent" in (await page.content()).lower():
            print("Email successfully sent")
        else:
            print("Send verification failed")
    except Exception as e:
        print(f"Send error: {str(e)}. Using keyboard shortcut")
        await page.keyboard.down("Control")
        await page.keyboard.press("Enter")
        await page.keyboard.up("Control")

async def google_search(page, query):
    """Perform a Google search and extract top results using Playwright."""
    google_url = "https://www.google.com"
    print(f"Performing Google search for: {query}")

    await page.goto(google_url, wait_until='load', timeout=90000)

    try:
        search_bar = page.locator('textarea[name="q"]')
        await search_bar.wait_for(state="visible", timeout=30000)
    except Exception as e:
        print(f"Failed to find search bar: {str(e)}")
        await page.screenshot(path="search_bar_failure.png")
        return []

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
            results.append({"title": title.strip(), "url": url})
    except Exception as e:
        print(f"Error extracting search results: {str(e)}")
        await page.screenshot(path="search_results_failure.png")

    return results

async def youtube_search_and_play(page, query):
    """Navigate to YouTube, search for a video, and play the first result."""
    youtube_url = "https://www.youtube.com"
    print(f"Navigating to YouTube and searching for: {query}")

    # Navigate to YouTube and wait for basic load
    await page.goto(youtube_url, wait_until='load', timeout=90000)

    # Wait for a stable element (YouTube logo) to ensure page is interactive
    try:
        await page.wait_for_selector('yt-icon#logo-icon', state="visible", timeout=60000)
        print("YouTube page is interactive")
    except Exception as e:
        print(f"Failed to confirm page interactivity: {str(e)}")
        await page.screenshot(path="youtube_page_load_failure.png")

    # Handle consent screen with broader detection
    try:
        consent_button = page.locator('button:visible', text=re.compile(r"(Accept|I agree)", re.I))
        if await consent_button.count():
            await consent_button.first.click()
            print("Consent screen accepted")
            await asyncio.sleep(3)  # Wait for page to settle
    except Exception as e:
        print(f"No consent screen or error handling it: {str(e)}")

    # Wait for the search bar to be visible
    try:
        search_bar = page.locator('ytd-searchbox input#search')
        await search_bar.wait_for(state="visible", timeout=60000)
        print("YouTube search bar found")
    except Exception as e:
        print(f"Failed to find YouTube search bar: {str(e)}")
        await page.screenshot(path="youtube_search_bar_failure.png")
        return

    # Enter the search query and submit
    await search_bar.click()
    await search_bar.type(query, delay=100)
    await page.keyboard.press("Enter")
    await page.wait_for_load_state('load', timeout=90000)

    # Click the first video result
    try:
        first_video = page.locator('ytd-video-renderer #video-title').first
        await first_video.wait_for(state="visible", timeout=30000)
        video_title = await first_video.text_content()
        print(f"Selecting first video: {video_title.strip()}")
        await first_video.click()
        await asyncio.sleep(5)  # Wait for the video to start playing
        print("Video is now playing")
    except Exception as e:
        print(f"Error selecting first video: {str(e)}")
        await page.screenshot(path="youtube_video_selection_failure.png")

    # Save a screenshot for debugging
    await page.screenshot(path="youtube_final_state.png")
    print("Screenshot saved as youtube_final_state.png")

async def run_gmail_agent():
    """Main function to run the Gmail agent with @search and @youtube features."""
    task = input("Enter your Gmail task, search query (@search), or YouTube video (@youtube): ").strip()

    if task.lower().startswith("@search"):
        search_query = task[len("@search"):].strip()
        if not search_query:
            print("Error: No search query provided after @search")
            return

        async with async_playwright() as p:
            chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            user_data_dir = os.path.expanduser("~/ChromeProfile")
            
            context = await p.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                executable_path=chrome_path,
                headless=False,
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

            try:
                results = await google_search(page, search_query)
                if results:
                    print(f"\nTop Google Search Results for '{search_query}':")
                    for i, result in enumerate(results, 1):
                        print(f"{i}. {result['title']}")
                        print(f"   {result['url']}")
                else:
                    print("No search results found.")
            except Exception as e:
                print(f"Search error: {str(e)}")
                await page.screenshot(path="search_error.png")
            
            await asyncio.sleep(5)
            print("Search process completed")
            await context.close()

    elif task.lower().startswith("@youtube"):
        youtube_query = task[len("@youtube"):].strip()
        if not youtube_query:
            print("Error: No YouTube query provided after @youtube")
            return

        async with async_playwright() as p:
            chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            # Temporarily remove user_data_dir to test with a fresh profile
            context = await p.chromium.launch_persistent_context(
                user_data_dir=os.path.expanduser("~/ChromeProfile"),
                executable_path=chrome_path,
                headless=False,
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

            try:
                await youtube_search_and_play(page, youtube_query)
            except Exception as e:
                print(f"YouTube error: {str(e)}")
                await page.screenshot(path="youtube_error.png")
            
            await asyncio.sleep(5)
            print("YouTube process completed")
            await context.close()

    else:
        details = extract_email_details(task)
        print(f"Details: {details}")

        async with async_playwright() as p:
            chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            user_data_dir = os.path.expanduser("~/ChromeProfile")
            
            context = await p.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                executable_path=chrome_path,
                headless=False,
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

            try:
                await send_gmail(page, details["email"], details["topic"], details["body"])
            except Exception as e:
                print(f"Main error: {str(e)}")
                await page.screenshot(path="main_error.png")
            
            await asyncio.sleep(5)
            print("Process completed")
            await context.close()

if __name__ == "__main__":
    asyncio.run(run_gmail_agent())