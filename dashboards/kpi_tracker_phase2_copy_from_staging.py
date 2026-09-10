#!/usr/bin/env python3
"""
kpi_tracker_phase2_copy_from_staging.py

PHASE 2 of the KPI Tracker pipeline (converted from KPITrackers.ipynb's
cell 14 into a regular script).

Phase 1 (kpi_tracker_phase1_download_update.py) downloads each tutor's KPI
Tracker, appends their metrics row, and saves the finished file locally to
    Desktop/KPI_Trackers/<Faculty Leader>/<Tutor> KPI Tracker.xlsx

Between phase 1 and this script, YOU manually upload the contents of each
Desktop/KPI_Trackers/<Faculty Leader>/ folder into that Faculty Leader's
"aa_Metrics_Upload" staging folder in SharePoint (General > IL Team Folders >
<Faculty Leader>'s Team > aa_Metrics_Upload). That's a plain drag-and-drop
browser upload -- nothing scripted touches the "Create or upload" button,
which is what kept crashing the browser before.

This script then, for each tutor:
  1. Finds "<Tutor> KPI Tracker.xlsx" in that Faculty Leader's aa_Metrics_Upload
     folder and right-clicks it.
  2. Clicks "Copy to", which opens SharePoint's file-picker iframe.
  3. Navigates Instructional Leadership > General > IL Team Folders >
     "<Faculty Leader>'s Team" > <Tutor's folder> > KPI Tracker.
  4. Clicks "Copy here", confirming the "Replace" prompt if SharePoint asks
     (the file already exists there from the previous version).
"""

import csv
import os
import threading
import time
import traceback
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException, TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

LOGIN_EMAIL = 'tyler.harrington@revolutionprep.com'
LOGIN_PASSWORD = 'Cattongue!950'

# =============================================================================
# CONFIG
# =============================================================================

# Ian Plamondon -- corrected trackers (+ Aaron/Zoe's fresh rows) uploaded
# to staging, ready to copy over.
FACULTY_LEADERS_TO_RUN = ['Ian Plamondon']
EXCLUDED_FACULTY_LEADERS = ['Katherine Marino', 'Nikki Pencak']

# Set this to re-run only specific tutors instead of every tutor for every
# leader -- handy after a run where most things succeeded and you just want
# to retry the ones that failed. Leave as None to run everyone normally.
# When set, ONLY the leaders listed here run, and ONLY the tutors listed for
# each one (FACULTY_LEADERS_TO_RUN and EXCLUDED_FACULTY_LEADERS are ignored
# while this is set). Reset to None here -- fill in with real names +
# `python3 kpi_tracker_phase2_copy_from_staging.py` after checking
# RESULTS_LOG_PATH for a run's failures.
# Tim Page and Eleanor Mancilla, Annelies de Groot's team -- one-off
# re-run for just these two.
RETRY_ONLY = {
    'Annelies de Groot': ['Tim Page', 'Eleanor Mancilla'],
}

MAX_TUTOR_ATTEMPTS = 3

# Phase 2 runs headless fine.
HEADLESS = True

# Forced to strictly 1 -- besides the earlier chromedriver -9 crash risk,
# 2+ concurrent automated sign-ins against the SAME account is what
# triggered Microsoft's "verify it's you" risk-based challenge (confirmed
# by screenshot), which has no automatable path through it. One at a
# time removes that risk entirely.
MAX_PARALLEL_LEADERS = 1

desktop_path = os.path.join(os.path.expanduser("~"), "Desktop")

# Shared across all 3 KPI tracker scripts on purpose (same directory name)
# -- see kpi_tracker_phase1_download_update.py's comment on this same
# constant. Clearing Microsoft's "verify it's you" step once, in any one
# of the 3 scripts, clears it for all three going forward.
PERSISTENT_PROFILE_DIR = os.path.join(desktop_path, ".automation_chrome_profile")
os.makedirs(PERSISTENT_PROFILE_DIR, exist_ok=True)

error_screenshot_dir = os.path.join(desktop_path, "automation_errors")
os.makedirs(error_screenshot_dir, exist_ok=True)

# Every tutor's outcome gets appended here THE INSTANT it's known, not just
# printed to the console -- this file is the source of truth even if the
# console output is lost, the run is interrupted, or the kernel crashes.
RESULTS_LOG_PATH = os.path.join(desktop_path, "copy_from_staging_results.csv")
RESULTS_LOCK = threading.Lock()
if not os.path.exists(RESULTS_LOG_PATH):
    with open(RESULTS_LOG_PATH, 'w', newline='') as f:
        csv.writer(f).writerow(['timestamp', 'faculty_leader', 'tutor', 'status', 'reason'])

# Full detail (str(e) with any chromedriver stacktrace, PLUS our own Python
# traceback) goes here instead of the console.
FULL_ERROR_LOG_PATH = os.path.join(error_screenshot_dir, "full_errors.log")


def log_full_error(context, e):
    with RESULTS_LOCK:
        with open(FULL_ERROR_LOG_PATH, 'a') as f:
            f.write('=' * 70 + '\n')
            f.write('{}  {}\n'.format(time.strftime('%Y-%m-%d %H:%M:%S'), context))
            f.write('-' * 70 + '\n')
            f.write(str(e) + '\n')
            f.write('--- python traceback ---\n')
            f.write(traceback.format_exc() + '\n')


def record_result(faculty_leader, tutor, status, reason=''):
    """Appends one row immediately and flushes to disk."""
    with RESULTS_LOCK:
        with open(RESULTS_LOG_PATH, 'a', newline='') as f:
            csv.writer(f).writerow([
                time.strftime('%Y-%m-%d %H:%M:%S'), faculty_leader, tutor, status, reason
            ])


MASTER_TUTOR_PATH = os.path.join(desktop_path, "Master_Tutor.csv")
MasterTutor = pd.read_csv(MASTER_TUTOR_PATH)

if RETRY_ONLY is not None:
    faculty_leaders = list(RETRY_ONLY.keys())
else:
    faculty_leaders = (
        FACULTY_LEADERS_TO_RUN
        if FACULTY_LEADERS_TO_RUN is not None
        else sorted(MasterTutor['Faculty Leader'].dropna().unique().tolist())
    )
    faculty_leaders = [fl for fl in faculty_leaders if fl not in EXCLUDED_FACULTY_LEADERS]

# =============================================================================
# HELPERS
# =============================================================================

FILES_CONTAINER_SELECTOR = (
    '#appRoot > div.Files.sp-App-root.has-footer.is-active.od-userSelect--enabled.'
    'sp-WebViewList-enable.sp-fullHeightLayouts > div.sp-App-bodyContainer > '
    'div.sp-App-body > div > div.Files-main > div.Files-mainColumn > '
    'div.Files-contentAreaFlexContainer > div > div > div'
)

def unicode_variants(s):
    """Returns [s, NFC-normalized, NFD-normalized], deduped. Guards against
    accented characters being stored as a different, visually-identical
    Unicode form in Master_Tutor.csv vs. the literal SharePoint file name
    -- XPath text() matching is an exact string comparison and silently
    treats these as NOT equal. (Same fix as phase 1.)"""
    variants = [s, unicodedata.normalize('NFC', s), unicodedata.normalize('NFD', s)]
    return list(dict.fromkeys(variants))


NAME_ALIASES = {
    'Joyraj Dsouza': "Joyraj D'souza",
    'Yvrine Nguiwenga Nketcha': 'Yvrine Nguiwenga-Nketcha',
    'Daniel Esquivel Reynoso': 'Daniel Esquivel-Reynoso',
    'Shaun ONeil': "Shaun O'Neil",
    'Cara Rabaev': "Cara Rabaev (McLaughlin)",
    'Jakob ONeal': "Jakob O'Neal",
    'Nayley Rolon Gomez': "Nayley Rolon-Gomez",
    # Master_Tutor.csv spells her "Nayely"; the actual SharePoint file is
    # spelled "Nayley" -- same fix as phase 1.
    'Nayely Rolon-Gomez': 'Nayley Rolon-Gomez',
}

DEAD_SESSION_MARKERS = (
    'Connection refused',
    'Max retries exceeded',
    'invalid session id',
    'session deleted',
    'chrome not reachable',
    'disconnected: not connected',
)


def is_dead_session_error(e):
    # NOTE: a blank/empty message is NOT a reliable crash signal -- Selenium's
    # WebDriverWait.until() raises a plain TimeoutException with an empty
    # message by default whenever no custom message is passed, which is true
    # of every wait in this script. Only the specific markers below (genuine
    # connection/session death) trigger a restart.
    text = str(e)
    return any(marker in text for marker in DEAD_SESSION_MARKERS)


# Python cannot forcibly stop a background thread -- Ctrl-C only raises
# KeyboardInterrupt in the MAIN thread. Any Faculty Leader thread that's
# mid-Selenium-call when you interrupt just keeps running, with its own live
# Chrome process. The only real way to stop it is to kill the actual browser
# out from under it, which makes its next Selenium call fail immediately and
# lets the thread exit on its own. Every driver gets registered here so that
# can happen.
ACTIVE_DRIVERS = []
ACTIVE_DRIVERS_LOCK = threading.Lock()


def register_driver(driver):
    with ACTIVE_DRIVERS_LOCK:
        ACTIVE_DRIVERS.append(driver)


def unregister_driver(driver):
    with ACTIVE_DRIVERS_LOCK:
        if driver in ACTIVE_DRIVERS:
            ACTIVE_DRIVERS.remove(driver)


def kill_all_drivers():
    with ACTIVE_DRIVERS_LOCK:
        drivers = list(ACTIVE_DRIVERS)
    for d in drivers:
        try:
            d.quit()
        except Exception:
            pass


# Resolved ONCE, up front, single-threaded, before any leader threads start.
# ChromeDriverManager().install() does file I/O against a shared cache
# directory (~/.wdm) -- calling it separately from every thread's
# make_driver() is a race the moment two threads hit it around the same time
# with a cold/empty cache. Every thread reuses this one resolved path.
DRIVER_PATH = ChromeDriverManager().install()


def make_driver():
    chrome_options = Options()
    chrome_options.add_argument(f'--user-data-dir={PERSISTENT_PROFILE_DIR}')
    if HEADLESS:
        chrome_options.add_argument('--headless=new')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--window-size=1920,1080')
    chrome_options.add_argument('--disable-dev-shm-usage')

    driver = webdriver.Chrome(service=Service(DRIVER_PATH), options=chrome_options)
    driver.set_window_size(1920, 1080)
    if not HEADLESS:
        driver.maximize_window()
    register_driver(driver)
    return driver


def safe_click(driver, element):
    try:
        element.click()
    except Exception:
        try:
            ActionChains(driver).send_keys(Keys.ESCAPE).perform()
            time.sleep(0.5)
            element.click()
        except Exception:
            driver.execute_script("arguments[0].click();", element)


def save_debug_screenshot(driver, tag):
    try:
        path = os.path.join(error_screenshot_dir, '{}_{}.png'.format(tag, int(time.time())))
        driver.save_screenshot(path)
        print(f'Saved debug screenshot: {path}')
    except Exception as e:
        print(f'Could not save debug screenshot: {e}')


def find_span_element(driver, text, max_scrolls=200, pause=0.4):
    """Same scroll-and-check approach phase 1 uses -- aa_Metrics_Upload is a
    normal SharePoint folder, same virtualized list."""
    container = driver.find_element(By.CSS_SELECTOR, FILES_CONTAINER_SELECTOR)
    xpath = '//span[text()="{}"]'.format(text)
    last_height = None
    for _ in range(max_scrolls):
        elements = driver.find_elements(By.XPATH, xpath)
        if elements:
            return elements[0]
        driver.execute_script(
            "arguments[0].scrollTop = arguments[0].scrollTop + arguments[0].clientHeight;", container
        )
        time.sleep(pause)
        new_height = driver.execute_script("return arguments[0].scrollHeight;", container)
        if new_height == last_height:
            time.sleep(1)
            elements = driver.find_elements(By.XPATH, xpath)
            if elements:
                return elements[0]
            break
        last_height = new_height
    raise NoSuchElementException('Could not locate "{}" after scrolling through the list.'.format(text))


def xpath_literal(s):
    """Safely embeds a string containing a single quote (e.g. "Joyraj
    D'souza") into an XPath expression via concat() if needed."""
    if "'" not in s:
        return "'{}'".format(s)
    if '"' not in s:
        return '"{}"'.format(s)
    parts = s.split("'")
    return "concat(" + ", \"'\", ".join("'{}'".format(p) for p in parts) + ")"


def sort_name_descending(driver):
    """Click the 'Name' column header, then 'Descending', to surface tutors
    further down the alphabet in the virtualized list."""
    name_header = WebDriverWait(driver, 5).until(
        EC.element_to_be_clickable((By.XPATH, "//div[@role='columnheader' and @title='Name']"))
    )
    safe_click(driver, name_header)
    time.sleep(2)
    descending_option = WebDriverWait(driver, 5).until(
        EC.element_to_be_clickable((By.XPATH, "//span[contains(text(),'Descending')]"))
    )
    safe_click(driver, descending_option)
    print("Sorted by descending. Retrying...")
    time.sleep(3)


def find_span_element_with_sort_fallback(driver, text, max_scrolls=200, pause=0.4):
    try:
        return find_span_element(driver, text, max_scrolls=max_scrolls, pause=pause)
    except NoSuchElementException as original_error:
        try:
            sort_name_descending(driver)
        except Exception as sort_error:
            print(f'Could not sort Z-A ({sort_error}) -- re-raising original error.')
            raise original_error
        return find_span_element(driver, text, max_scrolls=max_scrolls, pause=pause)


def find_first_matching_span(driver, candidate_texts):
    """Returns (element, matched_text) for the first candidate found."""
    last_error = None
    for text in candidate_texts:
        try:
            return find_span_element_with_sort_fallback(driver, text), text
        except NoSuchElementException as e:
            last_error = e
    raise last_error


def scroll_picker_list_to_bottom(driver, max_scrolls=70):
    """Inside the file-picker iframe: keep scrolling until the list stops
    growing, so every tutor folder is actually in the DOM before searching."""
    try:
        container = driver.find_element(By.TAG_NAME, "main")
    except Exception:
        container = driver.find_element(By.TAG_NAME, "body")

    try:
        scroll_height = driver.execute_script("return arguments[0].scrollHeight;", container)
        client_height = driver.execute_script("return arguments[0].clientHeight;", container)
    except Exception:
        return

    if scroll_height <= client_height:
        return

    last_height = scroll_height
    for _ in range(max_scrolls):
        driver.execute_script("arguments[0].scrollBy(0, 500);", container)
        time.sleep(0.6)
        new_height = driver.execute_script("return arguments[0].scrollHeight;", container)
        if new_height == last_height:
            break
        last_height = new_height


def click_primary_login_button(driver, timeout=20):
    """id="idSIButton9" is Microsoft's primary-action button ID, and it is
    REUSED across multiple different steps of this login wizard -- "Next"
    on the email page, "Sign in" on the password page, AND "Yes" on "Stay
    signed in?" all share this exact same ID. That's a real, confirmed
    problem, not a theoretical one: a screenshot from a live run caught it
    stuck on the password page with the password typed in but never
    submitted, while a sibling thread's click on "idSIButton9" raised a
    stale element error -- consistent with the click landing on the
    password page's button right as it was being replaced by the next
    step's (same-ID) button. Retries on a stale reference instead of
    giving up immediately."""
    last_error = None
    for _ in range(3):
        try:
            button = WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((By.ID, 'idSIButton9'))
            )
            button.click()
            return
        except StaleElementReferenceException as e:
            last_error = e
            time.sleep(1)
            continue
    raise last_error


def attempt_sign_in(driver, max_steps=8):
    """Signs in by detecting whatever screen is ACTUALLY on-screen at each
    step and reacting to it, instead of assuming a fixed email -> password
    -> KMSI sequence. That rigid sequence broke the moment the browser
    switched to a persistent profile (PERSISTENT_PROFILE_DIR): with an
    account already cached, Microsoft shows an account-picker tile instead
    of a blank email field, and depending on what's remembered it may skip
    straight past password entry too. Loops, re-reading page state each
    time, until it reaches "General" (logged in) or runs out of steps."""
    driver.get("https://revprepllc.sharepoint.com/sites/FacultyLeadership/Shared%20Documents")

    for step in range(max_steps):
        if driver.find_elements(By.XPATH, '//span[text()="General"]'):
            return  # already logged in

        # Gated behind "Pick an account" -- without this gate, the bare
        # LOGIN_EMAIL text match ALSO matches the small email breadcrumb
        # shown above the password field on the *password* page, which
        # sent the loop back to the email step in an endless loop instead
        # of ever reaching the password field (confirmed via screenshot).
        on_account_picker = driver.find_elements(By.XPATH, '//*[contains(text(),"Pick an account")]')
        if on_account_picker:
            tiles = driver.find_elements(By.XPATH, f'//*[normalize-space(text())="{LOGIN_EMAIL}"]')
            if tiles:
                safe_click(driver, tiles[0])
                print(f'Clicked cached account tile for {LOGIN_EMAIL}.')
                time.sleep(2)
                continue
            use_another = driver.find_elements(By.XPATH, '//*[contains(text(),"Use another account")]')
            if use_another:
                safe_click(driver, use_another[0])
                time.sleep(2)
                continue

        email_fields = [e for e in driver.find_elements(By.ID, 'i0116') if e.is_displayed()]
        if email_fields:
            email_fields[0].send_keys(LOGIN_EMAIL)
            click_primary_login_button(driver)
            time.sleep(2)
            continue

        password_fields = [e for e in driver.find_elements(By.ID, 'i0118') if e.is_displayed()]
        if password_fields:
            password_fields[0].send_keys(LOGIN_PASSWORD)
            click_primary_login_button(driver)
            time.sleep(2)
            continue

        if driver.find_elements(By.XPATH, '//*[text()="Stay signed in?"]'):
            click_primary_login_button(driver, timeout=10)
            print('Clicked "Stay signed in?" -> Yes.')
            time.sleep(2)
            continue

        time.sleep(2)

    print(f'attempt_sign_in: did not recognize a way forward after {max_steps} steps -- '
          f'login_and_reach_il_team_folders will catch/report it if this actually failed.')


def login_and_reach_il_team_folders(driver):
    attempt_sign_in(driver)

    general_button = None
    last_error = None
    for attempt in range(1, 4):
        try:
            general_button = WebDriverWait(driver, 40).until(
                EC.element_to_be_clickable((By.XPATH, '//span[text()="General"]'))
            )
            break
        except Exception as e:
            last_error = e
            if attempt < 3:
                print(f'Timed out waiting for "General" -- redoing sign-in and trying again ({attempt}/2)...')
                save_debug_screenshot(driver, f'phase2_login_general_timeout_attempt{attempt}')
                print(f'Was stuck on: {driver.current_url}')
                attempt_sign_in(driver)
    if general_button is None:
        save_debug_screenshot(driver, 'phase2_login_general_timeout_final')
        print(f'Stuck on: {driver.current_url}')
        raise TimeoutException(
            f'Timed out waiting for "General" even after redoing sign-in twice. Last error: {last_error}'
        )
    safe_click(driver, general_button)

    il_team_folders_button = WebDriverWait(driver, 40).until(
        EC.element_to_be_clickable((By.XPATH, '//span[text()="IL Team Folders"]'))
    )
    safe_click(driver, il_team_folders_button)
    time.sleep(2)

    return driver.current_url


def enter_team_folder(driver, il_team_folders_url, team_folder_name):
    driver.get(il_team_folders_url)
    time.sleep(3)
    team_button = find_span_element(driver, team_folder_name)
    safe_click(driver, team_button)
    time.sleep(3)
    return driver.current_url


def enter_staging_folder(driver, team_folder_url):
    """From a Faculty Leader's team folder, go into their aa_Metrics_Upload
    staging subfolder and return its URL."""
    driver.get(team_folder_url)
    time.sleep(3)
    staging_button = find_span_element(driver, "aa_Metrics_Upload")
    safe_click(driver, staging_button)
    time.sleep(3)
    return driver.current_url


def find_clickable_button_multi(driver, candidate_texts, timeout=20):
    """Tries each candidate spelling in turn -- a tutor's staging FILE and
    their destination FOLDER don't always use the same spelling (e.g.
    "Joyraj D'souza" vs. a folder named "Joyraj Dsouza")."""
    last_error = None
    for text in candidate_texts:
        try:
            xpath = "//button[contains(text(),{})]".format(xpath_literal(text))
            return WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((By.XPATH, xpath))
            ), xpath
        except Exception as e:
            last_error = e
    raise last_error


def click_with_stale_retry(driver, xpath, timeout=10, retries=3):
    """Finds an element by XPath and clicks it, retrying FROM SCRATCH
    (re-finding fresh) if it goes stale between being found and being
    clicked. The file-picker iframe's content re-renders on scroll and on
    nearby hover/focus changes, which can invalidate an element reference
    in that gap -- confirmed as a real, recurring bug (not a one-off):
    the exact same "stale element reference" error hit 4 different
    tutors across 4 separate runs before this fix, and the old code only
    had this protection on ONE of the picker's 6 click steps."""
    last_error = None
    for _ in range(retries):
        try:
            element = WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((By.XPATH, xpath))
            )
            driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", element)
            time.sleep(1)
            # Re-find fresh right before clicking -- the scroll animation
            # itself is what triggers the re-render in a lot of cases.
            element = WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((By.XPATH, xpath))
            )
            ActionChains(driver).move_to_element(element).click().perform()
            return
        except StaleElementReferenceException as e:
            last_error = e
            time.sleep(1)
            continue
    raise last_error


def copy_tutor_file_via_picker(driver, tutor, team_folder_name, file_text, folder_name_candidates=None):
    """Right-clicks the file span, clicks 'Copy to', then drives the
    file-picker iframe through to 'Copy here' + confirming 'Replace'.
    Raises on any failure -- caller handles retries."""
    if folder_name_candidates is None:
        folder_name_candidates = [tutor]
    file_button = find_span_element_with_sort_fallback(driver, file_text)
    ActionChains(driver).context_click(file_button).perform()
    time.sleep(2)
    safe_click(driver, driver.find_element(By.XPATH, "//span[text()='Copy to']"))
    time.sleep(3)

    iframe = WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "iframe[data-automationid='filePickerFrame']"))
    )
    driver.switch_to.frame(iframe)

    try:
        WebDriverWait(driver, 10).until(EC.visibility_of_element_located((By.ID, "appRoot")))

        click_with_stale_retry(
            driver,
            "//nav[@id='spartan-left-nav']//li//div[@name='Instructional Leadership']//a[@title='Instructional Leadership']"
        )
        time.sleep(2)

        click_with_stale_retry(driver, "//button[contains(text(),'General')]")
        time.sleep(2)

        click_with_stale_retry(driver, "//button[contains(text(),'IL Team Folders')]")
        time.sleep(2)

        click_with_stale_retry(driver, '//*[@title="{}"]'.format(team_folder_name))
        time.sleep(3)

        scroll_picker_list_to_bottom(driver)

        _, tutor_folder_xpath = find_clickable_button_multi(driver, folder_name_candidates)
        click_with_stale_retry(driver, tutor_folder_xpath, timeout=20)
        time.sleep(3)

        click_with_stale_retry(driver, "//button[contains(text(),'KPI Tracker')]", timeout=20)
        time.sleep(3)

        click_with_stale_retry(driver, "//button[@data-automationid='picker-complete']")
        time.sleep(3)
    finally:
        driver.switch_to.default_content()

    try:
        WebDriverWait(driver, 8).until(
            EC.presence_of_element_located((By.XPATH, '//div[contains(@class, "toastInnerContainer")]'))
        )
        replace_button = WebDriverWait(driver, 8).until(
            EC.element_to_be_clickable((By.XPATH, '//button[@name="Replace"]'))
        )
        driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", replace_button)
        safe_click(driver, replace_button)
        time.sleep(2)
    except Exception:
        pass  # no collision prompt -- fine, nothing to confirm


def short_error(e):
    """Just the first line of str(e) for the console -- the full
    chromedriver stack trace goes to FULL_ERROR_LOG_PATH instead."""
    return str(e).splitlines()[0] if str(e).splitlines() else str(e)


def process_tutor(driver, tutor, staging_folder_url, team_folder_name, max_attempts=MAX_TUTOR_ATTEMPTS):
    """Returns (success, reason). reason is None on success."""
    # Unicode NFC/NFD variants -- fixes accented names (e.g. the "e" in
    # "Monet") being stored as a different, visually-identical Unicode
    # form between Master_Tutor.csv and the literal SharePoint file name.
    # This was already fixed in phase 1 but hadn't been ported here yet
    # (confirmed: "Q. Monét Travis" failed here the same way it did there).
    candidates = list(dict.fromkeys(
        variant
        for base in [
            tutor,
            NAME_ALIASES.get(tutor, tutor),
            tutor.replace("'", "").replace("'", "").replace("-", " "),
        ]
        for variant in unicode_variants(base)
    ))

    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            driver.switch_to.default_content()
            driver.get(staging_folder_url)
            time.sleep(3)

            _, file_text = find_first_matching_span(
                driver, ['{} KPI Tracker.xlsx'.format(c) for c in candidates]
            )
            copy_tutor_file_via_picker(driver, tutor, team_folder_name, file_text, folder_name_candidates=candidates)
            print(f'Copied "{tutor}" from staging to their KPI Tracker folder.')
            return True, None

        except Exception as e:
            last_error = e
            log_full_error(f'process_tutor "{tutor}" attempt {attempt}/{max_attempts}', e)
            print(f'Attempt {attempt}/{max_attempts} failed for tutor "{tutor}": {short_error(e)}')
            if is_dead_session_error(e):
                return False, 'browser session crashed'
            if attempt < max_attempts:
                backoff = 5 * attempt
                print(f'Waiting {backoff}s before retrying "{tutor}"...')
                time.sleep(backoff)

    save_debug_screenshot(driver, tutor.replace(' ', '_'))
    print(f'Giving up on tutor "{tutor}" after {max_attempts} attempts. Last error: {short_error(last_error) if last_error else "unknown"}')
    return False, short_error(last_error) if last_error else 'unknown error'


def run_faculty_leader(faculty_leader, start_delay=0):
    if start_delay:
        time.sleep(start_delay)

    print(f'[{faculty_leader}] starting...')
    local_failures = []
    driver = None
    try:
        driver = make_driver()
        il_team_folders_url = login_and_reach_il_team_folders(driver)
        team_folder_name = "{}'s Team".format(faculty_leader.split()[0])

        try:
            team_folder_url = enter_team_folder(driver, il_team_folders_url, team_folder_name)
            staging_folder_url = enter_staging_folder(driver, team_folder_url)
        except NoSuchElementException as e:
            print(f'[{faculty_leader}] Could not reach aa_Metrics_Upload ({short_error(e)}) -- skipping.')
            record_result(faculty_leader, '(entire team)', 'FAILED', 'staging folder not found')
            return [(faculty_leader, '(entire team)', 'staging folder not found')]

        tutor_list = (
            MasterTutor[MasterTutor['Faculty Leader'] == faculty_leader]['Full Name']
            .dropna()
            .tolist()
        )
        if RETRY_ONLY is not None:
            wanted = set(RETRY_ONLY.get(faculty_leader, []))
            tutor_list = [t for t in tutor_list if t in wanted]
            print(f'[{faculty_leader}] RETRY_ONLY set -- running just {len(tutor_list)} tutor(s): {tutor_list}')

        for tutor in tutor_list:
            print(f'[{faculty_leader}] {tutor}')
            success, reason = process_tutor(driver, tutor, staging_folder_url, team_folder_name)

            if success:
                record_result(faculty_leader, tutor, 'SUCCESS')
            else:
                local_failures.append((faculty_leader, tutor, reason))
                record_result(faculty_leader, tutor, 'FAILED', reason)

                if reason == 'browser session crashed':
                    print(f'[{faculty_leader}] Restarting the browser after a crash...')
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    unregister_driver(driver)
                    try:
                        driver = make_driver()
                        il_team_folders_url = login_and_reach_il_team_folders(driver)
                        team_folder_url = enter_team_folder(driver, il_team_folders_url, team_folder_name)
                        staging_folder_url = enter_staging_folder(driver, team_folder_url)
                        print(f'[{faculty_leader}] Browser restarted successfully, continuing...')
                        continue
                    except Exception as restart_error:
                        log_full_error(f'[{faculty_leader}] browser restart failed', restart_error)
                        print(
                            f'[{faculty_leader}] Could not restart the browser ({short_error(restart_error)}) -- '
                            f'marking all remaining tutors for this leader as not attempted.'
                        )
                        remaining = tutor_list[tutor_list.index(tutor) + 1:]
                        for remaining_tutor in remaining:
                            local_failures.append(
                                (faculty_leader, remaining_tutor, 'not attempted -- browser restart failed')
                            )
                            record_result(
                                faculty_leader, remaining_tutor, 'FAILED', 'not attempted -- browser restart failed'
                            )
                        break

    except Exception as e:
        log_full_error(f'[{faculty_leader}] fatal error', e)
        print(f'[{faculty_leader}] Fatal error, aborting this leader: {short_error(e)}')
        local_failures.append((faculty_leader, '(entire team)', f'fatal error: {short_error(e)}'))
        record_result(faculty_leader, '(entire team)', 'FAILED', f'fatal error: {short_error(e)}')
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
            unregister_driver(driver)
        print(f'[{faculty_leader}] done.')

    return local_failures


def main():
    worker_count = MAX_PARALLEL_LEADERS if MAX_PARALLEL_LEADERS else len(faculty_leaders)

    all_failed_tutors = []
    interrupted = False

    # NOT using "with ThreadPoolExecutor(...) as executor" on purpose --
    # that context manager's __exit__ calls shutdown(wait=True), which
    # blocks on every thread finishing BEFORE a KeyboardInterrupt ever
    # reaches the except block below. Managing shutdown manually lets us
    # kill every active Chrome window FIRST, which makes each thread's next
    # Selenium call fail immediately and lets it exit on its own.
    executor = ThreadPoolExecutor(max_workers=worker_count)
    futures = {
        # Wider stagger avoids multiple leaders hitting SharePoint's SSO
        # login concurrently, which looks like a code bug but is really
        # login contention.
        executor.submit(run_faculty_leader, fl, i * 15): fl
        for i, fl in enumerate(faculty_leaders)
    }
    try:
        for future in as_completed(futures):
            fl = futures[future]
            try:
                failures = future.result()
                all_failed_tutors.extend(failures)
            except Exception as e:
                print(f'[{fl}] worker crashed: {e}')
                all_failed_tutors.append((fl, '(entire team)', f'worker crashed: {e}'))
        executor.shutdown(wait=True)
    except KeyboardInterrupt:
        interrupted = True
        print()
        print('Interrupted -- closing every active browser now so nothing keeps running in the background...')
        kill_all_drivers()
        executor.shutdown(wait=True, cancel_futures=True)
        print("All browsers closed. Every tutor's outcome up to this point was already saved to RESULTS_LOG_PATH.")

    print('All Faculty Leaders processed.' if not interrupted else 'Run was interrupted before all leaders finished.')
    print()
    print('=' * 60)
    if all_failed_tutors:
        print(f'{len(all_failed_tutors)} tutor(s) did NOT get copied to their destination folder (from threads that finished reporting):')
        print()
        current_leader = None
        for leader, tutor, reason in sorted(all_failed_tutors, key=lambda x: x[0]):
            if leader != current_leader:
                print(f'{leader}:')
                current_leader = leader
            print(f'  - {tutor}  ({reason})')
        print()
        print('Debug screenshots for these are in: {}'.format(error_screenshot_dir))
        print('These still need to be moved out of aa_Metrics_Upload by hand.')
    elif not interrupted:
        print('Every tutor was copied successfully. No failures to report.')
    print('=' * 60)
    print()
    print(
        'Full per-tutor results (SUCCESS/FAILED, written live as each one finished, '
        'survives crashes/interrupts) are in: {}'.format(RESULTS_LOG_PATH)
    )


if __name__ == "__main__":
    main()
