import argparse
import json
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright


X_BASE_URL = "https://x.com"


def normalize_username(username):
    username = username.strip()

    if username.startswith("@"):
        username = username[1:]

    username = username.strip("/")

    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", username):
        raise ValueError(f"Invalid X username: {username}")

    return username


def get_post_id_from_href(href):
    if not href:
        return None

    match = re.search(r"/status/(\d+)", href)

    if match:
        return match.group(1)

    return None


def get_post_url(href):
    if not href:
        return None

    return urljoin(X_BASE_URL, href)


def extract_media(article):
    media = []

    # ---------------------------------------------------------
    # Images
    # ---------------------------------------------------------

    images = article.locator('img')

    for i in range(images.count()):
        try:
            img = images.nth(i)

            src = img.get_attribute("src")
            alt = img.get_attribute("alt")

            if not src:
                continue

            # Ignore avatars/profile images where possible
            if alt and "profile" in alt.lower():
                continue

            # X CDN images generally contain media/upload
            if "pbs.twimg.com/media" in src:
                media.append({
                    "type": "photo",
                    "url": src
                })

        except Exception:
            continue

    # ---------------------------------------------------------
    # Videos
    # ---------------------------------------------------------

    videos = article.locator("video")

    for i in range(videos.count()):
        try:
            video = videos.nth(i)

            src = video.get_attribute("src")
            poster = video.get_attribute("poster")

            item = {
                "type": "video",
                "url": src,
                "thumbnail": poster
            }

            # Don't add empty media
            if src or poster:
                media.append(item)

        except Exception:
            continue

    # ---------------------------------------------------------
    # Deduplicate
    # ---------------------------------------------------------

    unique = []

    seen = set()

    for item in media:
        key = (
            item.get("type"),
            item.get("url"),
            item.get("thumbnail")
        )

        if key in seen:
            continue

        seen.add(key)
        unique.append(item)

    return unique


def scrape_x(username, limit=5):
    username = normalize_username(username)

    profile_url = f"{X_BASE_URL}/{username}"

    posts = []
    seen_ids = set()

    errors = []

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        context = browser.new_context(
            viewport={
                "width": 1280,
                "height": 900
            },
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )

        page = context.new_page()

        try:

            print(f"Opening {profile_url}")

            response = page.goto(
                profile_url,
                wait_until="domcontentloaded",
                timeout=30000
            )

            if response:
                print("HTTP status:", response.status)

            page.wait_for_timeout(5000)

            # -------------------------------------------------
            # Detect obvious access problems
            # -------------------------------------------------

            current_url = page.url

            print("Final URL:", current_url)

            body_text = page.locator("body").inner_text(
                timeout=10000
            )

            lower_body = body_text.lower()

            possible_block = (
                "something went wrong" in lower_body
                or "log in" in lower_body
                or "sign in" in lower_body
                or "account doesn't exist" in lower_body
            )

            if possible_block:
                print(
                    "Warning: page may be restricted or "
                    "require authentication."
                )

            # -------------------------------------------------
            # Find tweets
            # -------------------------------------------------

            for scroll_number in range(6):

                articles = page.locator(
                    'article[data-testid="tweet"]'
                )

                count = articles.count()

                print(
                    f"Scroll {scroll_number}: "
                    f"found {count} tweet elements"
                )

                for i in range(count):

                    if len(posts) >= limit:
                        break

                    try:

                        article = articles.nth(i)

                        # -------------------------------------
                        # Tweet ID
                        # -------------------------------------

                        links = article.locator(
                            'a[href*="/status/"]'
                        )

                        tweet_id = None
                        tweet_href = None

                        for j in range(links.count()):

                            href = links.nth(j).get_attribute(
                                "href"
                            )

                            found_id = get_post_id_from_href(
                                href
                            )

                            if found_id:

                                tweet_id = found_id
                                tweet_href = href

                                break

                        if not tweet_id:
                            continue

                        if tweet_id in seen_ids:
                            continue

                        # -------------------------------------
                        # Text
                        # -------------------------------------

                        text = ""

                        text_locator = article.locator(
                            '[data-testid="tweetText"]'
                        )

                        if text_locator.count() > 0:

                            text = text_locator.first.inner_text(
                                timeout=3000
                            )

                        # -------------------------------------
                        # Date
                        # -------------------------------------

                        created_at = None

                        time_locator = article.locator("time")

                        if time_locator.count() > 0:

                            created_at = (
                                time_locator.first
                                .get_attribute("datetime")
                            )

                        # -------------------------------------
                        # Media
                        # -------------------------------------

                        media = extract_media(article)

                        # -------------------------------------
                        # Build result
                        # -------------------------------------

                        post = {
                            "id": tweet_id,
                            "username": username,
                            "text": text,
                            "url": get_post_url(tweet_href),
                            "created_at": created_at,
                            "media": media,
                        }

                        posts.append(post)
                        seen_ids.add(tweet_id)

                        print(
                            f"Collected {len(posts)}/{limit}: "
                            f"{tweet_id}"
                        )

                    except Exception as e:

                        print(
                            "Error parsing tweet:",
                            repr(e)
                        )

                if len(posts) >= limit:
                    break

                # Scroll
                page.mouse.wheel(0, 1500)

                page.wait_for_timeout(2000)

            # -------------------------------------------------
            # Final validation
            # -------------------------------------------------

            if len(posts) == 0:

                errors.append(
                    "No posts were found. "
                    "X may have returned a login/challenge/"
                    "different page structure."
                )

        except Exception as e:

            errors.append(str(e))

        finally:

            context.close()
            browser.close()

    result = {
        "success": len(posts) > 0,
        "username": username,
        "requested": limit,
        "count": len(posts),
        "posts": posts,
        "errors": errors,
        "scraped_at": datetime.now(
            timezone.utc
        ).isoformat(),
    }

    return result


def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--username",
        required=True
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=5
    )

    parser.add_argument(
        "--output",
        default="result.json"
    )

    args = parser.parse_args()

    try:

        result = scrape_x(
            args.username,
            args.limit
        )

        with open(
            args.output,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                result,
                f,
                ensure_ascii=False,
                indent=2
            )

        print(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2
            )
        )

        if not result["success"]:
            sys.exit(2)

    except Exception as e:

        error = {
            "success": False,
            "error": str(e)
        }

        with open(
            args.output,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                error,
                f,
                ensure_ascii=False,
                indent=2
            )

        print(json.dumps(error))

        sys.exit(1)


if __name__ == "__main__":
    main()
