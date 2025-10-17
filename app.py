import os
import json
import re
import subprocess
import time
import base64
import shutil
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# Load environment variables (you can also hardcode for testing)
load_dotenv()

# Use the given secret or from env
MY_SECRET = os.getenv("MY_SECRET", "well-that-is-a-secret")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GITHUB_USER = os.getenv("GITHUB_USER")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

# Validate GitHub config
if not GITHUB_USER or not GITHUB_TOKEN:
    raise RuntimeError("GITHUB_USER and GITHUB_TOKEN must be set")

# Setup OpenAI client if available
try:
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)
    OPENAI_SDK_PRESENT = True
except ImportError:
    client = None
    OPENAI_SDK_PRESENT = False

app = Flask(__name__)


@app.route('/api/build', methods=['POST'])
def handle_build_request():
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400
    if data.get('secret') != MY_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    # required fields
    for fld in ("email", "task", "round", "nonce", "brief", "evaluation_url"):
        if fld not in data:
            return jsonify({"error": f"Missing {fld}"}), 400

    print(f"[Task={data['task']}] Valid request received (round={data['round']})")
    try:
        process_task(data)
    except Exception as e:
        # We still return 200 per spec, but log error
        print(f"[Task={data['task']}] Error: {e}")

    return jsonify({"message": "Received"}), 200


def process_task(data):
    task = data['task']
    round_num = int(data['round'])
    brief = data['brief']
    attachments = data.get('attachments', [])
    repo_name = "tds-proj1"  # fixed to your project
    repo_path = os.path.join("/tmp", repo_name)

    print(f"[{task}] Processing round {round_num}")

    # decode attachments
    attachments_dir = None
    if attachments:
        attachments_dir = os.path.join("/tmp", f"{repo_name}-attachments")
        if os.path.exists(attachments_dir):
            shutil.rmtree(attachments_dir)
        os.makedirs(attachments_dir, exist_ok=True)
        for att in attachments:
            name = att.get("name")
            url = att.get("url")
            if name and url:
                if url.startswith("data:"):
                    _, b64 = url.split(",", 1)
                    content = base64.b64decode(b64)
                    with open(os.path.join(attachments_dir, name), "wb") as f:
                        f.write(content)
                else:
                    # try HTTP fetch
                    try:
                        r = requests.get(url, timeout=10)
                        r.raise_for_status()
                        with open(os.path.join(attachments_dir, name), "wb") as f:
                            f.write(r.content)
                    except Exception as e:
                        print(f"[{task}] Warning: failed to fetch attachment {name}: {e}")

    # Generate or update code
    generated = generate_code_with_llm(brief, attachments, existing_repo_path=(repo_path if round_num > 1 else None))
    if not generated:
        raise RuntimeError("Failed to generate code from LLM")

    # Clean old clone
    if os.path.exists(repo_path):
        shutil.rmtree(repo_path)
    os.makedirs(repo_path, exist_ok=True)

    # Write files
    for fi in generated:
        fname = fi.get("name")
        content = fi.get("content", "")
        target = os.path.join(repo_path, fname)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)

    # Ensure LICENSE (MIT) exists
    licpath = os.path.join(repo_path, "LICENSE")
    if not os.path.exists(licpath):
        year = time.gmtime().tm_year
        mit = f"""MIT License

Copyright (c) {year} {GITHUB_USER}

Permission is hereby granted...
"""
        with open(licpath, "w", encoding="utf-8") as f:
            f.write(mit)

    # Git init / commit
    run_cmd(['git', 'init'], repo_path)
    run_cmd(['git', 'checkout', '-b', 'main'], repo_path)
    run_cmd(['git', 'add', '.'], repo_path)
    run_cmd(['git', 'commit', '-m', f"Round {round_num} commit"], repo_path)

    # remote URL with token
    remote = f'https://{GITHUB_USER}:{GITHUB_TOKEN}@github.com/{GITHUB_USER}/{repo_name}.git'
    try:
        run_cmd(['git', 'remote', 'add', 'origin', remote], repo_path)
    except Exception:
        run_cmd(['git', 'remote', 'set-url', 'origin', remote], repo_path)

    run_cmd(['git', 'push', '-u', 'origin', 'main', '--force'], repo_path)

    # publish pages
    pages_url = f"https://{GITHUB_USER}.github.io/{repo_name}/"
    try:
        run_cmd(['gh', 'pages', 'publish', '--branch', 'main', f'--repo={GITHUB_USER}/{repo_name}'], repo_path)
    except Exception as e:
        print(f"[{task}] gh pages publish error: {e}")

    # wait for pages
    ok = wait_for_pages_ok(pages_url, timeout=300)
    if not ok:
        print(f"[{task}] Warning: Pages not responding OK after wait")

    # commit SHA
    commit_sha = run_cmd(['git', 'rev-parse', 'HEAD'], repo_path).strip()
    repo_url = f"https://github.com/{GITHUB_USER}/{repo_name}"

    print(f"[{task}] Deployed. Commit {commit_sha}, pages {pages_url}")

    # notify evaluator
    notify_evaluator(data, repo_url, commit_sha, pages_url)
    print(f"[{task}] Notification sent.")


def generate_code_with_llm(brief, attachments, existing_repo_path=None):
    # fallback if no client
    if client is None:
        print("No LLM client; returning minimal stub")
        return [
            {"name": "index.html", "content": f"<!doctype html><html><body><h1>{brief}</h1></body></html>"},
            {"name": "README.md", "content": f"# {brief}\n\nAuto-generated stub."},
            {"name": "LICENSE", "content": "MIT License (placeholder)"}
        ]

    prompt = f"You are an expert web developer. Here is the brief:\n{brief}\n"
    if attachments:
        prompt += "Attachments: " + ", ".join(att.get("name","") for att in attachments) + "\n"
    if existing_repo_path and os.path.exists(existing_repo_path):
        prompt += "You must modify existing code. Show file diffs or full files accordingly.\n"
        # optionally include some content from existing files (first few lines)
        for root, _, files in os.walk(existing_repo_path):
            if '.git' in root:
                continue
            for f in files:
                try:
                    with open(os.path.join(root, f), 'r', encoding='utf-8', errors='ignore') as fr:
                        snippet = fr.read(500)
                        prompt += f"\n-- {f} snippet --\n{snippet}\n"
                except:
                    pass

    prompt += """
INSTRUCTIONS:
Output exactly a JSON object with key "files" whose value is an array of { name, content } objects.
Include index.html, README.md, LICENSE. No extra text outside JSON.
"""

    try:
        if hasattr(client, "chat"):
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1500,
            )
            raw = resp.choices[0].message.content
        else:
            resp = client.responses.create(
                model="gpt-4o-mini",
                input=prompt,
                max_tokens=1500,
            )
            raw = getattr(resp, "output_text", str(resp))
        json_text = extract_json_from_text(raw)
        if not json_text:
            json_text = extract_json_from_text(raw, allow_loose=True)
        parsed = json.loads(json_text)
        return parsed.get("files", [])
    except Exception as e:
        print(f"LLM error or invalid output: {e}")
        return None


def extract_json_from_text(text, allow_loose=False):
    if not text:
        return None
    # remove fences
    text = re.sub(r"```(?:json|js|html)?\n?", "", text)
    text = re.sub(r"```", "", text)
    # find balanced JSON
    start_idxs = [m.start() for m in re.finditer(r"\{", text)]
    for s in start_idxs:
        stack = 0
        for i in range(s, len(text)):
            if text[i] == "{":
                stack += 1
            elif text[i] == "}":
                stack -= 1
                if stack == 0:
                    cand = text[s:i+1]
                    try:
                        json.loads(cand)
                        return cand
                    except:
                        break
    if allow_loose:
        try:
            cleaned = text.strip()
            first = min(pos for pos in (cleaned.find('{'), cleaned.find('[')) if pos != -1)
            last = max(cleaned.rfind('}'), cleaned.rfind(']'))
            cand = cleaned[first:last+1]
            json.loads(cand)
            return cand
        except:
            return None
    return None


def run_cmd(cmd, cwd):
    print(f"Running: {' '.join(cmd)} (cwd={cwd})")
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"Command failed: {cmd}\nstderr: {res.stderr}\nstdout: {res.stdout}")
    return res.stdout.strip()


def wait_for_pages_ok(url, timeout=300):
    print(f"Waiting for {url} (timeout {timeout}s)")
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = requests.get(url, timeout=5)
            print(f"Status {r.status_code}")
            if r.status_code == 200:
                return True
        except:
            pass
        time.sleep(2)
    return False


def notify_evaluator(orig, repo_url, commit_sha, pages_url):
    payload = {
        "email": orig['email'],
        "task": orig['task'],
        "round": orig['round'],
        "nonce": orig['nonce'],
        "repo_url": repo_url,
        "commit_sha": commit_sha,
        "pages_url": pages_url,
    }
    url = orig['evaluation_url']
    headers = {'Content-Type': 'application/json'}
    delay = 1
    for attempt in range(1, 6):
        print(f"Notify attempt {attempt} to {url}")
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=10)
            if r.status_code == 200:
                print("Evaluator notified successfully")
                return
            else:
                print(f"Notify failed status {r.status_code}, body: {r.text}")
        except Exception as e:
            print(f"Notify exception {e}")
        time.sleep(delay)
        delay *= 2
    raise RuntimeError("Failed notifying evaluator after retries.")


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
