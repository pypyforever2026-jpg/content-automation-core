
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

        # Add user profile
        chrome_options.add_argument(f"--user-data-dir={self.profile_path}")

        # Additional options for stability
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)

        if self.headless:
            chrome_options.add_argument("--headless")

        try:
            # Initialize driver
            self.driver = webdriver.Chrome(options=chrome_options)
            self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            # Setup explicit wait
            self.wait = WebDriverWait(self.driver, 30)

            self.logger.info("Chrome driver initialized successfully")

        except WebDriverException as e:
            self.logger.error(f"Failed to initialize Chrome driver: {e}")
            raise

    def login(self):
        """
        Navigate to YouTube Studio and handle login if needed
        User will manually login on first run, profile will be saved for future use
        """
        self.logger.info("Navigating to YouTube Studio...")

        # Go directly to YouTube Studio upload page
        self.driver.get("https://www.youtube.com/upload")

        # Check if we're on login page or upload page
        current_url = self.driver.current_url

        if "accounts.google.com" in current_url or "signin" in current_url:
            self.logger.info("Please log in manually in the browser window...")
            self.logger.info("After logging in, the script will continue automatically")

            # Wait for user to complete login and redirect to upload page
            try:
                self.wait.until(EC.url_contains("youtube.com/upload"))
                self.logger.info("Login successful! Redirected to upload page")
            except TimeoutException:
                self.logger.warning("Still on login page after waiting. Please check login manually.")

        elif "youtube.com/upload" in current_url or "studio.youtube.com" in current_url:
            self.logger.info("Already logged in! Proceeding with upload...")

        else:
            self.logger.warning(f"Unexpected page: {current_url}")

        # Give page time to fully load
        time.sleep(5)

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
        """Upload the video file"""
        try:
            # Find file input and upload
            file_input = self.wait.until(
                EC.presence_of_element_located((By.XPATH, "//input[@type='file']"))
            )
            file_input.send_keys(os.path.abspath(video_path))

            # Wait for upload progress to appear and then disappear
            self.logger.info("Waiting for file upload to complete...")

            # Wait for the upload progress to start
            try:
                self.wait.until(
                    EC.presence_of_element_located((By.XPATH, "//*[contains(text(), 'Uploading') or contains(text(), 'Processing')]"))
                )
                self.logger.info("Upload started...")
            except TimeoutException:
                pass  # Upload might be very fast

            # Wait for upload to complete (progress bar disappears)
            try:
                self.wait.until(
                    EC.invisibility_of_element_located((By.XPATH, "//*[contains(text(), 'Uploading')]"))
                )
            except TimeoutException:
                pass

            self.logger.info("File upload completed")
            return True

        except TimeoutException:
            self.logger.error("Failed to upload file - timeout")
            return False
        except Exception as e:
            self.logger.error(f"Failed to upload file: {e}")
            return False

    def _fill_video_details(self, title, description):
        """Fill in video title and description"""
        try:
            # Wait for the details form to load
            time.sleep(6)

            # Fill title - try multiple selectors
            title_selectors = [
                "//div[@id='textbox' and @aria-label='Add a title that describes your video (type @ to mention a channel)']",
                "//div[@id='textbox'][contains(@aria-label, 'title')]",
                "//ytcp-mention-textbox[@label='Title']//div[@id='textbox']",
                "//div[@id='textbox' and @contenteditable='true']"
            ]

            title_filled = False
            for selector in title_selectors:
                try:
                    title_field = self.wait.until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                    title_field.click()
                    title_field.clear()
                    title_field.send_keys(Keys.CONTROL + "a")  # Select all
                    title_field.send_keys(title)
                    self.logger.info("Title filled successfully")
                    title_filled = True
                    break
                except TimeoutException:
                    continue

            if not title_filled:
                self.logger.warning("Could not fill title with any selector")

            # Fill description - try multiple selectors
            description_selectors = [
                "//div[@id='textbox' and @aria-label='Tell viewers about your video (type @ to mention a channel)']",
                "//div[@id='textbox'][contains(@aria-label, 'description')]",
                "//ytcp-mention-textbox[@label='Description']//div[@id='textbox']"
            ]

            description_filled = False
            for selector in description_selectors:
                try:
                    description_field = self.wait.until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                    description_field.click()
                    description_field.clear()
                    description_field.send_keys(Keys.CONTROL + "a")  # Select all
                    description_field.send_keys(description)
                    self.logger.info("Description filled successfully")
                    description_filled = True
                    break
                except TimeoutException:
                    continue

            if not description_filled:
                self.logger.warning("Could not fill description with any selector")

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
