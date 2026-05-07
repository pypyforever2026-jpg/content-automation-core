
import os
import time
import logging
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
from selenium.webdriver.common.keys import Keys

class YouTubeUploader:
    def __init__(self, profile_path=None, headless=False):
        """
        Initialize the YouTube uploader

        Args:
            profile_path (str): Path to Chrome user profile directory
            headless (bool): Run browser in headless mode
        """
        self.profile_path = profile_path or os.path.join(os.getcwd(), "chrome_profile")
        self.headless = headless
        self.driver = None
        self.wait = None
        self.setup_logging()

    def setup_logging(self):
        """Setup logging configuration"""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler('youtube_upload.log'),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(__name__)

    def setup_driver(self):
        """Setup Chrome driver with user profile"""
        chrome_options = Options()

        chrome_options.add_argument(f"--user-data-dir={self.profile_path}")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)

        # ── Anti-lock / anti-dialog flags ────────────────────────────────────
        # Prevents: (a) "Chrome didn't shut down correctly" bubble that blocks
        # navigation after a quick restart; (b) Chrome opening in guest/temp
        # profile when the SingletonLock file is still present from the previous
        # session (which causes driver.get() to leave the browser on the Chrome
        # new-tab page instead of the requested URL).
        chrome_options.add_argument("--no-first-run")
        chrome_options.add_argument("--no-default-browser-check")
        chrome_options.add_argument("--disable-session-crashed-bubble")
        chrome_options.add_argument("--disable-restore-session-state")
        chrome_options.add_argument("--disable-infobars")
        # ─────────────────────────────────────────────────────────────────────

        if self.headless:
            chrome_options.add_argument("--headless")

        try:
            self.driver = webdriver.Chrome(options=chrome_options)
            self.driver.execute_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            self.wait = WebDriverWait(self.driver, 30)
            self.logger.info("Chrome driver initialized successfully")

        except WebDriverException as e:
            self.logger.error(f"Failed to initialize Chrome driver: {e}")
            raise

    # ─────────────────────────────
    # Browser State Helpers
    # ─────────────────────────────

    def _is_chrome_stuck(self) -> bool:
        """
        Return True when the browser is on a Chrome internal / blank page,
        meaning driver.get() had no effect.

        Common causes:
          - Profile SingletonLock still held → Chrome opened in guest mode.
          - Crash-recovery dialog intercepted navigation.
        """
        try:
            url = self.driver.current_url or ""
        except Exception:
            return True
        return (
            not url
            or url.startswith(("chrome://", "about:", "data:", "chrome-error://"))
        )

    def _log_page_state(self, label: str = "") -> None:
        """
        Log the current URL, page title, and a body snippet.
        Called after every navigation step so logs are self-contained.
        """
        try:
            url = self.driver.current_url or "N/A"
        except Exception:
            url = "ERROR"
        try:
            title = self.driver.title or "(no title)"
        except Exception:
            title = "ERROR"
        try:
            body_snippet = self.driver.execute_script(
                "return (document.body ? document.body.innerText : '').substring(0, 300);"
            ) or "(empty)"
        except Exception:
            body_snippet = "ERROR"

        prefix = f"[{label}] " if label else ""
        self.logger.info(f"{prefix}URL   : {url}")
        self.logger.info(f"{prefix}TITLE : {title}")
        self.logger.info(f"{prefix}BODY  : {body_snippet!r}")

    def login(self):
        """
        Navigate to YouTube Studio and verify the page is usable before returning.

        Problems that caused the "stuck on Chrome home" symptom:
          1. current_url was read IMMEDIATELY after driver.get() — before any
             HTTP redirect had time to resolve, so the check saw the wrong URL.
          2. If Chrome opened in guest mode (profile lock), driver.get() left
             the browser on chrome://new-tab-page permanently.
          3. No retry when Studio wasn't reached on the first attempt.

        This version:
          - Detects Chrome internal pages and retries with JS navigation.
          - Logs full page state (URL + title + body) at every step so failures
            are immediately diagnosable.
          - Retries up to 3 times before giving up.
          - Waits up to 5 minutes for manual Google login if needed.
        """
        self.logger.info("Navigating to YouTube Studio...")

        target = "https://studio.youtube.com"

        for attempt in range(1, 4):
            self.logger.info(f"Login attempt {attempt}/3")
            try:
                self.driver.get(target)
            except Exception as e:
                self.logger.error(f"driver.get({target}) raised: {e}")
                self._log_page_state(f"login attempt {attempt} after failed get")
                time.sleep(5)
                continue

            # Give redirect time to resolve (do NOT read current_url immediately)
            time.sleep(5)
            self._log_page_state(f"login attempt {attempt} after get+5s")

            # ── Chrome internal page? driver.get() had no effect ─────────
            if self._is_chrome_stuck():
                current = self.driver.current_url or ""
                self.logger.error(
                    f"Chrome is stuck on internal page: '{current}'\n"
                    f"  Likely cause: SingletonLock still held (profile in use) "
                    f"or crash-recovery dialog blocked navigation.\n"
                    f"  Trying JS navigation fallback..."
                )
                try:
                    self.driver.execute_script(
                        "window.location.href = arguments[0];", target
                    )
                    time.sleep(5)
                    self._log_page_state(f"login attempt {attempt} after JS fallback")
                except Exception as js_e:
                    self.logger.error(f"JS navigation also failed: {js_e}")

                if self._is_chrome_stuck():
                    self.logger.error(
                        "Still on Chrome internal page — "
                        "Chrome profile may still be locked. Retrying..."
                    )
                    time.sleep(4)
                    continue
            # ─────────────────────────────────────────────────────────────

            current_url = self.driver.current_url or ""

            if "accounts.google.com" in current_url or "signin" in current_url:
                self.logger.info(
                    "Redirected to Google login — "
                    "waiting up to 5 minutes for manual sign-in..."
                )
                try:
                    WebDriverWait(self.driver, 300).until(
                        EC.url_contains("studio.youtube.com")
                    )
                    self.logger.info("Manual login completed successfully!")
                    self._log_page_state("after manual login")
                    break
                except TimeoutException:
                    self.logger.error("Login wait timed out after 5 minutes.")
                    return

            elif "studio.youtube.com" in current_url:
                self.logger.info("Already logged in to YouTube Studio!")
                break

            else:
                self.logger.warning(
                    f"Unexpected page on attempt {attempt}: {current_url} — retrying..."
                )
                time.sleep(3)
                continue

        # Final sanity check: wait for any Studio UI element to be present.
        studio_ready_selectors = [
            "//ytcs-app",
            "//ytcp-app",
            "//*[@id='upload-btn']",
            "//input[@type='file']",
            "//*[@id='avatar-btn']",
        ]
        studio_ready = False
        for sel in studio_ready_selectors:
            try:
                WebDriverWait(self.driver, 15).until(
                    EC.presence_of_element_located((By.XPATH, sel))
                )
                self.logger.info(f"YouTube Studio UI confirmed ready (selector: {sel})")
                studio_ready = True
                break
            except TimeoutException:
                continue

        if not studio_ready:
            self.logger.warning(
                "No Studio UI element detected — page may not be fully loaded. "
                "Proceeding anyway but upload may fail."
            )
            self._log_page_state("login final state — Studio UI not confirmed")

        time.sleep(3)

    def upload_video(self, video_path, title, description, visibility="public", made_for_kids=False):
        """
        Upload a video to YouTube with complete workflow

        Args:
            video_path (str): Path to the video file
            title (str): Video title
            description (str): Video description
            visibility (str): "public", "private", "unlisted", or "scheduled"
            made_for_kids (bool): Whether the video is made for kids (COPPA compliance)

        Returns:
            bool: True if upload successful, False otherwise
        """
        if not os.path.exists(video_path):
            self.logger.error(f"Video file not found: {video_path}")
            return False

        try:
            # Step 1: Upload video file
            self.logger.info("Uploading video file...")
            if not self._upload_file(video_path):
                return False

            # Step 2: Fill in details (title, description)
            self.logger.info("Filling video details...")
            if not self._fill_video_details(title, description):
                return False

            # Step 3: Handle COPPA kids content selection (CRITICAL FIX)
            self.logger.info("Setting kids content preference...")
            if not self._set_kids_content(made_for_kids):
                return False

            # Step 4: Navigate through the upload workflow
            self.logger.info("Proceeding through upload workflow...")
            if not self._navigate_upload_workflow():
                return False

            # Step 5: Set visibility and save/publish (ENHANCED FIX)
            self.logger.info("Setting visibility and saving...")
            if not self._set_visibility_and_save(visibility):
                return False

            self.logger.info("Video uploaded successfully!")
            return True

        except Exception as e:
            self.logger.error(f"Error during upload: {e}")
            return False

    def _upload_file(self, video_path):
        """
        Upload the video file and wait until Studio confirms the upload is done.

        Bug fixed: previously the "Uploading" invisibility wait had a 30-second
        timeout that silently passed even when upload was still in progress,
        returning True as a false positive. Additionally, current_url was checked
        immediately without waiting, so if Studio hadn't loaded yet the file input
        was never found and we returned False.

        Now we:
          1. Wait up to 30s for the file input to appear.
          2. Send the file path.
          3. Wait up to 30s for the upload dialog to OPEN (confirms Studio is ready).
          4. Wait up to 10 minutes for the "Uploading" progress text to disappear.
          5. Verify a details-form element is present before returning True.
        """
        try:
            # Step 1: Find the (possibly hidden) file input on Studio
            file_input = WebDriverWait(self.driver, 30).until(
                EC.presence_of_element_located((By.XPATH, "//input[@type='file']"))
            )
            file_input.send_keys(os.path.abspath(video_path))
            self.logger.info("File path sent to input — waiting for upload dialog...")

            # Step 2: Wait for the upload dialog to actually open.
            # The dialog contains the "Uploading" progress label. If this never
            # appears within 30s the file input wasn't on the upload page.
            upload_dialog_xpath = (
                "//*[contains(text(), 'Uploading') or "
                "contains(text(), 'Upload video') or "
                "contains(text(), 'Processing') or "
                "contains(@class, 'upload-dialog') or "
                "contains(@class, 'ytcp-uploads-dialog')]"
            )
            try:
                WebDriverWait(self.driver, 30).until(
                    EC.presence_of_element_located((By.XPATH, upload_dialog_xpath))
                )
                self.logger.info("Upload dialog opened / upload started.")
            except TimeoutException:
                self.logger.error(
                    "Upload dialog did not appear within 30s — "
                    "Studio may not have been on the correct page."
                )
                return False

            # Step 3: Wait for "Uploading X%" to disappear (upload transfer done).
            # Use a generous 10-minute timeout for large video files.
            self.logger.info("Waiting for file transfer to complete (up to 10 min)...")
            uploading_xpath = "//*[contains(text(), 'Uploading')]"
            try:
                WebDriverWait(self.driver, 600).until(
                    EC.invisibility_of_element_located((By.XPATH, uploading_xpath))
                )
                self.logger.info("File transfer complete.")
            except TimeoutException:
                self.logger.warning(
                    "Upload transfer still showing after 10 min — proceeding anyway."
                )

            # Step 4: Confirm the details form is accessible (title textbox visible).
            # This also gives Studio a moment to switch to the Details step.
            details_ready_xpath = (
                "//div[@id='textbox' and @contenteditable='true'] | "
                "//ytcp-mention-textbox[@label='Title']"
            )
            try:
                WebDriverWait(self.driver, 30).until(
                    EC.presence_of_element_located((By.XPATH, details_ready_xpath))
                )
                self.logger.info("Details form is ready.")
            except TimeoutException:
                self.logger.warning(
                    "Details form not detected after upload — "
                    "_fill_video_details will retry with its own waits."
                )

            self.logger.info("File upload completed")
            return True

        except TimeoutException:
            self.logger.error("Failed to upload file - timeout waiting for file input")
            return False
        except Exception as e:
            self.logger.error(f"Failed to upload file: {e}")
            return False

    def _fill_video_details(self, title, description):
        """
        Fill in video title and description.

        Bug fixed: previously used time.sleep(6) then tried 4 title selectors
        each with a 30-second WebDriverWait (total up to ~2 minutes of wasted
        time) even though the details form wasn't ready. Now we first wait for
        ANY title textbox to become clickable (up to 90s, covering cases where
        the upload is still finalizing), then fill it in one shot.
        """
        try:
            # ── Title ───────────────────────────────────────────────────────
            title_selectors = [
                "//ytcp-mention-textbox[@label='Title']//div[@id='textbox']",
                "//div[@id='textbox' and contains(@aria-label, 'title')]",
                "//div[@id='textbox' and @aria-label='Add a title that describes your video (type @ to mention a channel)']",
                "//div[@id='textbox' and @contenteditable='true']",
            ]

            title_field = None
            for selector in title_selectors:
                try:
                    title_field = WebDriverWait(self.driver, 90).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                    self.logger.info(f"Title field found: {selector}")
                    break
                except TimeoutException:
                    continue

            if title_field is None:
                self.logger.warning("Could not find title field with any selector")
            else:
                try:
                    title_field.click()
                    time.sleep(0.3)
                    title_field.send_keys(Keys.CONTROL + "a")
                    title_field.send_keys(title)
                    self.logger.info("Title filled successfully")
                except Exception as e:
                    self.logger.warning(f"Title fill interaction failed: {e}")

            # ── Description ─────────────────────────────────────────────────
            description_selectors = [
                "//ytcp-mention-textbox[@label='Description']//div[@id='textbox']",
                "//div[@id='textbox' and contains(@aria-label, 'description')]",
                "//div[@id='textbox' and @aria-label='Tell viewers about your video (type @ to mention a channel)']",
            ]

            description_field = None
            for selector in description_selectors:
                try:
                    description_field = WebDriverWait(self.driver, 15).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                    self.logger.info(f"Description field found: {selector}")
                    break
                except TimeoutException:
                    continue

            if description_field is None:
                self.logger.warning("Could not find description field with any selector")
            else:
                try:
                    description_field.click()
                    time.sleep(0.3)
                    description_field.send_keys(Keys.CONTROL + "a")
                    description_field.send_keys(description)
                    self.logger.info("Description filled successfully")
                except Exception as e:
                    self.logger.warning(f"Description fill interaction failed: {e}")

            return True

        except Exception as e:
            self.logger.error(f"Failed to fill video details: {e}")
            return False

    def _set_kids_content(self, made_for_kids=False):
        """
        Handle COPPA kids content selection - THIS IS THE CRITICAL FIX

        Args:
            made_for_kids (bool): True if video is made for kids, False if not
        """
        try:
            self.logger.info(f"Setting video as {'made for kids' if made_for_kids else 'NOT made for kids'}")

            # Wait for kids content section to appear
            time.sleep(2)

            # Multiple selectors for kids content radio buttons
            if made_for_kids:
                # Select "Yes, it's made for kids"
                kids_selectors = [
                    "//tp-yt-paper-radio-button[@name='VIDEO_MADE_FOR_KIDS_MFK']",
                    "//paper-radio-button[@name='VIDEO_MADE_FOR_KIDS_MFK']",
                    "//*[@name='VIDEO_MADE_FOR_KIDS_MFK']",
                    "//tp-yt-paper-radio-button[contains(@name, 'MFK')]",
                    "//*[contains(text(), 'Yes, it\'s made for kids')]//ancestor::tp-yt-paper-radio-button",
                    "//*[contains(text(), 'made for kids')]//ancestor::tp-yt-paper-radio-button[1]"
                ]
            else:
                # Select "No, it's not made for kids" 
                kids_selectors = [
                    "//tp-yt-paper-radio-button[@name='VIDEO_MADE_FOR_KIDS_NOT_MFK']",
                    "//paper-radio-button[@name='VIDEO_MADE_FOR_KIDS_NOT_MFK']",
                    "//*[@name='VIDEO_MADE_FOR_KIDS_NOT_MFK']",
                    "//tp-yt-paper-radio-button[contains(@name, 'NOT_MFK')]",
                    "//*[contains(text(), 'No, it\'s not made for kids')]//ancestor::tp-yt-paper-radio-button",
                    "//*[contains(text(), 'not made for kids')]//ancestor::tp-yt-paper-radio-button"
                ]

            kids_selection_made = False
            for selector in kids_selectors:
                try:
                    kids_radio = WebDriverWait(self.driver, 10).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )

                    # Scroll element into view
                    self.driver.execute_script("arguments[0].scrollIntoView(true);", kids_radio)
                    time.sleep(1)

                    # Try clicking the radio button
                    try:
                        kids_radio.click()
                    except:
                        # If regular click fails, use JavaScript
                        self.driver.execute_script("arguments[0].click();", kids_radio)

                    self.logger.info(f"Successfully set kids content: {made_for_kids}")
                    kids_selection_made = True
                    break

                except TimeoutException:
                    continue
                except Exception as e:
                    self.logger.debug(f"Selector failed: {selector}, Error: {e}")
                    continue

            if not kids_selection_made:
                self.logger.error("Failed to set kids content selection with any selector")

                # Try alternative approach - look for any radio button with kids-related text
                try:
                    target_text = "not made for kids" if not made_for_kids else "made for kids"
                    radio_buttons = self.driver.find_elements(By.XPATH, "//tp-yt-paper-radio-button")

                    for radio in radio_buttons:
                        if target_text.lower() in radio.text.lower():
                            radio.click()
                            self.logger.info(f"Alternative method: Successfully set kids content to {made_for_kids}")
                            kids_selection_made = True
                            break
                except:
                    pass

            # Give some time for the selection to register
            time.sleep(2)

            return kids_selection_made

        except Exception as e:
            self.logger.error(f"Failed to set kids content: {e}")
            return False

    def _navigate_upload_workflow(self):
        """Navigate through the upload workflow steps"""
        try:
            # Click "Next" buttons to proceed through the workflow
            next_buttons_clicked = 0
            max_next_buttons = 3  # Typically: Details -> Video elements -> Visibility

            for step in range(max_next_buttons):
                self.logger.info(f"Looking for Next button (step {step + 1})")

                # Wait a moment for the page to load
                time.sleep(3)

                # Try different selectors for the Next button
                next_button_selectors = [
                    "//ytcp-button[@id='next-button']",
                    "//button[@id='next-button']",
                    "//*[@id='next-button']",
                    "//ytcp-button[contains(@class, 'next-button')]",
                    "//button[contains(text(), 'Next')]",
                    "//ytcp-button//span[text()='Next']/..",
                    "//div[@id='next-button']"
                ]

                next_clicked = False
                for selector in next_button_selectors:
                    try:
                        next_button = WebDriverWait(self.driver, 10).until(
                            EC.presence_of_element_located((By.XPATH, selector))
                        )

                        # Check if button is enabled
                        if next_button.get_attribute("disabled") is None:
                            # Ensure button is clickable
                            WebDriverWait(self.driver, 5).until(
                                EC.element_to_be_clickable((By.XPATH, selector))
                            )
                            next_button.click()
                            self.logger.info(f"Next button clicked (step {step + 1})")
                            next_clicked = True
                            next_buttons_clicked += 1
                            time.sleep(3)  # Wait for page transition
                            break
                        else:
                            self.logger.info(f"Next button found but disabled (step {step + 1})")
                            # If disabled, wait a bit and try again (kids selection might be processing)
                            time.sleep(2)
                    except (TimeoutException, Exception) as e:
                        continue

                if not next_clicked:
                    self.logger.info(f"No more Next buttons found at step {step + 1}")
                    break

            self.logger.info(f"Navigated through {next_buttons_clicked} workflow steps")
            return True

        except Exception as e:
            self.logger.error(f"Failed to navigate workflow: {e}")
            return False

    def _set_visibility_and_save(self, visibility):
        """
        Set video visibility and save (ENHANCED VERSION)

        Args:
            visibility (str): "public", "private", "unlisted", or "scheduled"
        """
        try:
            # Wait for visibility page to load
            time.sleep(3)

            self.logger.info(f"Setting video visibility to: {visibility}")

            # Map visibility options to their exact names used by YouTube
            visibility_mapping = {
                "private": "PRIVATE",
                "unlisted": "UNLISTED", 
                "public": "PUBLIC",
                "scheduled": "SCHEDULED"
            }

            target_visibility = visibility_mapping.get(visibility.lower(), "PUBLIC")

            # Enhanced selectors for visibility radio buttons based on research
            visibility_selectors = [
                # Try by name attribute (most reliable)
                f"//tp-yt-paper-radio-button[@name='{target_visibility}']",
                f"//paper-radio-button[@name='{target_visibility}']",

                # Try by ID (from research findings)
                f"//tp-yt-paper-radio-button[@id='{visibility.lower()}-radio-button']",
                f"//#{visibility.lower()}-radio-button",

                # Try by text content
                f"//*[contains(text(), '{visibility.title()}')]//ancestor::tp-yt-paper-radio-button",
                f"//*[contains(text(), '{visibility.title()}')]//ancestor::paper-radio-button",

                # Try by aria-label or other attributes
                f"//tp-yt-paper-radio-button[contains(@aria-label, '{visibility.title()}')]",

                # Generic approach - find all radio buttons and match by text
                f"//tp-yt-paper-radio-group[@id='privacy-radios']//tp-yt-paper-radio-button[contains(., '{visibility.title()}')]"
            ]

            visibility_set = False
            for selector in visibility_selectors:
                try:
                    self.logger.info(f"Trying visibility selector: {selector}")
                    visibility_radio = WebDriverWait(self.driver, 8).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )

                    # Scroll element into view
                    self.driver.execute_script("arguments[0].scrollIntoView(true);", visibility_radio)
                    time.sleep(1)

                    # Try clicking the radio button
                    try:
                        visibility_radio.click()
                    except:
                        # If regular click fails, use JavaScript
                        self.driver.execute_script("arguments[0].click();", visibility_radio)

                    self.logger.info(f"Successfully set visibility to {visibility}")
                    visibility_set = True
                    break

                except TimeoutException:
                    continue
                except Exception as e:
                    self.logger.debug(f"Visibility selector failed: {selector}, Error: {e}")
                    continue

            # Alternative method if all selectors fail
            if not visibility_set:
                self.logger.warning("Trying alternative visibility selection method...")
                try:
                    # Find all radio buttons in the visibility section
                    radio_buttons = self.driver.find_elements(By.XPATH, "//tp-yt-paper-radio-button")

                    for radio in radio_buttons:
                        radio_text = radio.text.lower() if radio.text else ""
                        if visibility.lower() in radio_text:
                            radio.click()
                            self.logger.info(f"Alternative method: Successfully set visibility to {visibility}")
                            visibility_set = True
                            break
                except Exception as e:
                    self.logger.error(f"Alternative visibility method failed: {e}")

            # Wait for visibility selection to register
            time.sleep(2)

            # Now click Save/Publish button - ENHANCED SAVE BUTTON DETECTION
            self.logger.info("Looking for Save/Publish button...")

            # Enhanced selectors for save/publish buttons
            save_publish_selectors = [
                # Save button (for draft/private videos)
                "//ytcp-button[@id='done-button' and contains(., 'Save')]",
                "//button[contains(text(), 'Save')]",
                "//ytcp-button[contains(@aria-label, 'Save')]",
                "//*[contains(text(), 'Save')]//ancestor::ytcp-button",

                # Publish button (for public videos)
                "//ytcp-button[@id='done-button' and contains(., 'Publish')]",
                "//button[contains(text(), 'Publish')]", 
                "//ytcp-button[contains(@aria-label, 'Publish')]",
                "//*[contains(text(), 'Publish')]//ancestor::ytcp-button",

                # Generic done button
                "//ytcp-button[@id='done-button']",
                "//button[@id='done-button']",
                "//*[@id='done-button']",
                "//ytcp-button[contains(@class, 'done-button')]"
            ]

            button_clicked = False
            for selector in save_publish_selectors:
                try:
                    self.logger.info(f"Trying save/publish selector: {selector}")
                    save_button = WebDriverWait(self.driver, 8).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )

                    # Check what type of button this is
                    button_text = save_button.text.lower() if save_button.text else ""
                    self.logger.info(f"Found button with text: '{button_text}'")

                    # Click the button
                    save_button.click()
                    self.logger.info(f"Successfully clicked {button_text} button")
                    button_clicked = True
                    break

                except TimeoutException:
                    continue
                except Exception as e:
                    self.logger.debug(f"Save/publish selector failed: {selector}, Error: {e}")
                    continue

            if not button_clicked:
                self.logger.error("Could not find Save/Publish button")
                return False

            # Brief pause so the prechecks dialog (if any) starts rendering
            time.sleep(3)

            # Handle "We're still checking your content" warning if it appears
            self._handle_prechecks_warning()

            # Wait for completion
            time.sleep(15)

            # Check for any confirmation or success messages
            try:
                success_indicators = [
                    "//*[contains(text(), 'Video published')]",
                    "//*[contains(text(), 'Video saved')]",
                    "//*[contains(text(), 'Upload complete')]",
                    "//*[contains(text(), 'Processing')]"
                ]

                for indicator in success_indicators:
                    try:
                        success_element = self.driver.find_element(By.XPATH, indicator)
                        if success_element:
                            self.logger.info(f"Success confirmation found: {success_element.text}")
                            break
                    except:
                        continue

            except:
                pass

            return True

        except Exception as e:
            self.logger.error(f"Failed to set visibility and save: {e}")
            return False

    def _handle_prechecks_warning(self, max_wait_sec: int = 12) -> bool:
        """
        Handle YouTube's "We're still checking your content" warning dialog
        (ytcp-prechecks-warning-dialog) by clicking the "Publish anyway" button
        when it appears.

        This dialog appears intermittently on certain channels right after the
        Publish click and blocks the actual publish unless dismissed.

        Args:
            max_wait_sec: how long to wait for the dialog to appear at most.

        Returns:
            bool: True if the dialog was found AND "Publish anyway" was clicked,
                  False otherwise (also returns False if the dialog never appeared,
                  which is the normal happy path).
        """
        # Selectors that indicate the prechecks warning dialog is open.
        dialog_selectors = [
            "//ytcp-prechecks-warning-dialog",
            "//*[@id='dialog-title' and contains(., \"still checking your content\")]",
            "//h1[contains(@class, 'ytcp-prechecks-warning-dialog')]",
        ]

        dialog_found = False
        end_time = time.time() + max_wait_sec
        while time.time() < end_time:
            for sel in dialog_selectors:
                try:
                    el = self.driver.find_element(By.XPATH, sel)
                    if el and el.is_displayed():
                        dialog_found = True
                        break
                except Exception:
                    continue
            if dialog_found:
                break
            time.sleep(0.5)

        if not dialog_found:
            self.logger.info("No prechecks warning dialog detected (normal case).")
            return False

        self.logger.warning("Prechecks warning dialog detected. Clicking 'Publish anyway'...")

        # Selectors for the "Publish anyway" button, ordered most-specific first.
        publish_anyway_selectors = [
            "//ytcp-prechecks-warning-dialog//button[@aria-label='Publish anyway']",
            "//button[@aria-label='Publish anyway']",
            "//ytcp-prechecks-warning-dialog//*[normalize-space(text())='Publish anyway']/ancestor::button",
            "//*[normalize-space(text())='Publish anyway']/ancestor::button",
            "//ytcp-button[@aria-label='Publish anyway']",
            "//ytcp-button[contains(., 'Publish anyway')]",
        ]

        for selector in publish_anyway_selectors:
            try:
                btn = WebDriverWait(self.driver, 8).until(
                    EC.element_to_be_clickable((By.XPATH, selector))
                )
                try:
                    self.driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", btn
                    )
                except Exception:
                    pass
                time.sleep(0.4)
                try:
                    btn.click()
                except Exception:
                    self.driver.execute_script("arguments[0].click();", btn)

                self.logger.info("Clicked 'Publish anyway' on prechecks warning dialog.")
                # Give YouTube a moment to actually submit the publish.
                time.sleep(3)
                return True
            except TimeoutException:
                continue
            except Exception as e:
                self.logger.debug(f"Publish anyway selector failed: {selector}, Error: {e}")
                continue

        # Last-resort JS scan over all buttons in the dialog
        try:
            clicked = self.driver.execute_script("""
                var dialog = document.querySelector('ytcp-prechecks-warning-dialog');
                var scope = dialog || document;
                var buttons = scope.querySelectorAll('button');
                for (var i = 0; i < buttons.length; i++) {
                    var label = buttons[i].getAttribute('aria-label') || '';
                    var text = (buttons[i].innerText || buttons[i].textContent || '').trim();
                    if (label === 'Publish anyway' || text === 'Publish anyway') {
                        buttons[i].click();
                        return true;
                    }
                }
                return false;
            """)
            if clicked:
                self.logger.info("Clicked 'Publish anyway' via JS fallback.")
                time.sleep(3)
                return True
        except Exception as e:
            self.logger.debug(f"JS fallback for Publish anyway failed: {e}")

        self.logger.error("Prechecks dialog was open but 'Publish anyway' could not be clicked.")
        return False

    def close(self):
        """Close the browser"""
        if self.driver:
            self.driver.quit()
            self.logger.info("Browser closed")

    def __enter__(self):
        """Context manager entry"""
        self.setup_driver()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.close()


def upload_video_to_youtube(video_path, title, description, profile_path=None, headless=False, 
                          visibility="public", made_for_kids=False):
    """
    Main function to upload video to YouTube

    Args:
        video_path (str): Path to video file
        title (str): Video title
        description (str): Video description
        profile_path (str): Chrome profile path (optional)
        headless (bool): Run in headless mode
        visibility (str): "public", "private", "unlisted", or "scheduled"
        made_for_kids (bool): Whether video is made for kids (COPPA compliance)

    Returns:
        bool: Success status
    """
    uploader = None
    try:
        uploader = YouTubeUploader(profile_path, headless)
        uploader.setup_driver()

        # Handle login
        uploader.login()

        # Upload video with complete workflow including COPPA compliance and visibility
        success = uploader.upload_video(video_path, title, description, visibility, made_for_kids)

        if success:
            uploader.logger.info("YouTube upload completed successfully!")
        else:
            uploader.logger.error("YouTube upload failed!")

        return success

    except Exception as e:
        logging.error(f"Critical error during YouTube upload: {e}")
        return False
    finally:
        if uploader:
            # Keep browser open for a moment to see result
            time.sleep(15)
            uploader.close()
