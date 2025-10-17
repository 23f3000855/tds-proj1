
import os
import json
import subprocess
import time
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

MY_SECRET = os.getenv("MY_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GITHUB_USER = os.getenv("GITHUB_USER")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")


client = OpenAI(api_key=OPENAI_API_KEY)


app = Flask(__name__)


@app.route('/api/build', methods=['POST'])
def handle_build_request():
    
    data = request.get_json()
    if not data or data.get('secret') != MY_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    print(f"Received valid request for task: {data.get('task')}, round: {data.get('round')}")
    process_task(data)

    return jsonify({"message": "Request received and is being processed."}), 200



def process_task(data):
    """
    Handles the main logic: generate code, deploy, and notify.
    """
    task_id = data['task']
    round_num = data['round']
    brief = data['brief']
    attachments = data.get('attachments', [])
    repo_name = task_id 
    repo_path = os.path.join('/tmp', repo_name) 

    print(f"[{task_id}] Starting round {round_num}...")

    try:
        
        print(f"[{task_id}] Generating code with LLM...")
        generated_files = generate_code_with_llm(brief, attachments, repo_path if round_num > 1 else None)
        if not generated_files:
            raise Exception("LLM failed to generate files.")

        
        print(f"[{task_id}] Deploying to GitHub...")
        if round_num == 1:
            
            deploy_new_repo(repo_name, repo_path, generated_files)
        else:
            
            update_existing_repo(repo_name, repo_path, generated_files)
        
        
        commit_sha = subprocess.check_output(['git', '-C', repo_path, 'rev-parse', 'HEAD']).decode().strip()
        pages_url = f"https://23f3000855.github.io/tds-proj1/"
        repo_url = f"https://github.com/23f3000855/tds-proj1"
        
        print(f"[{task_id}] Deployment successful. Commit: {commit_sha}")
        print(f"[{task_id}] Pages URL: {pages_url}")

        
        print(f"[{task_id}] Notifying evaluation server...")
        notify_evaluator(data, repo_url, commit_sha, pages_url)
        print(f"[{task_id}] Process completed successfully.")

    except Exception as e:
        print(f"[{task_id}] An error occurred: {e}")



def generate_code_with_llm(brief, attachments, existing_repo_path=None):
    """
    Uses an LLM to generate application files based on the brief.
    For round 2+, it reads existing files to provide context for modifications.
    """
    prompt_content = f"You are an expert web developer. Your task is to build a single-page web application based on the following brief.\n\n"
    prompt_content += f"BRIEF:\n{brief}\n\n"

    if attachments:
        prompt_content += "The following attachments are provided as data URIs:\n"
        for att in attachments:
            prompt_content += f"- {att['name']}\n"
    
    
    if existing_repo_path and os.path.exists(existing_repo_path):
        prompt_content += "\n--- EXISTING CODE ---\n"
        prompt_content += "You must modify the following existing files. Do not start from scratch.\n"
        for root, _, files in os.walk(existing_repo_path):
            if '.git' in root:
                continue
            for filename in files:
                try:
                    with open(os.path.join(root, filename), 'r', encoding='utf-8') as f:
                        file_content = f.read()
                        prompt_content += f"\n-- File: {filename} --\n{file_content}\n"
                except Exception:
                    pass 
        prompt_content += "--- END EXISTING CODE ---\n\n"

    prompt_content += """
    INSTRUCTIONS:
    1.  Generate all necessary code. For simplicity, create a single `index.html` file with inline CSS and JavaScript if possible.
    2.  Create a professional `README.md` file explaining the project, setup, and usage.
    3.  Create an `LICENSE` file with the MIT License text.
    4.  Your response MUST be a JSON object with a single key "files", which is an array of objects. Each object must have two keys: "name" (the filename) and "content" (the file's source code).
    5.  Do not include any explanations or conversational text outside of the JSON structure.

    Example JSON output:
    {
      "files": [
        { "name": "index.html", "content": "<!DOCTYPE html>..." },
        { "name": "README.md", "content": "
        { "name": "LICENSE", "content": "MIT License..." }
      ]
    }
    """
    
    try:
        completion = client.chat.completions.create(
            model="gpt-4-turbo-preview", 
            messages=[{"role": "user", "content": prompt_content}],
            response_format={"type": "json_object"}
        )
        response_json = json.loads(completion.choices[0].message.content)
        return response_json.get("files", [])
    except Exception as e:
        print(f"Error calling LLM: {e}")
        return None
    


def run_command(command, working_dir):
    """Helper to run a shell command and check for errors."""
    result = subprocess.run(command, cwd=working_dir, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(f"Command failed: {' '.join(command)}\nError: {result.stderr}")
    return result.stdout.strip()

def deploy_new_repo(repo_name, repo_path, files):
    """Creates a new GitHub repo, pushes files, and enables GitHub Pages."""
    
    if os.path.exists(repo_path):
        subprocess.run(['rm', '-rf', repo_path])
    
    os.makedirs(repo_path)

    
    for file_info in files:
        with open(os.path.join(repo_path, file_info['name']), 'w') as f:
            f.write(file_info['content'])

    
    try:
        
        os.environ['GITHUB_TOKEN'] = GITHUB_TOKEN
        run_command(['gh', 'repo', 'create', repo_name, '--public', f'--source={repo_path}'], '/tmp')
    except Exception as e:
        
        if "already exists" not in str(e):
            raise e
        print(f"Repo {repo_name} already exists. Will overwrite.")

    run_command(['git', 'init'], repo_path)
    run_command(['git', 'add', '.'], repo_path)
    run_command(['git', 'commit', '-m', 'Initial commit'], repo_path)
    run_command(['git', 'branch', '-M', 'main'], repo_path)
    run_command(['git', 'remote', 'add', 'origin', f'https://23f3000855:GITHUB_TOKEN@github.com/23f3000855/tds-proj1.git'], repo_path)
    run_command(['git', 'push', '-u', 'origin', 'main', '--force'], repo_path)
    
    
    run_command(['gh', 'pages', 'publish', '--branch', 'main', f'--repo=23f3000855/tds-proj1'], repo_path)

def update_existing_repo(repo_name, repo_path, files):
    """Clones an existing repo, updates files, and pushes changes."""
    
    if os.path.exists(repo_path):
        subprocess.run(['rm', '-rf', repo_path])
        
    
    run_command(['git', 'clone', clone_url, repo_path], '/tmp')

    
    for file_info in files:
        with open(os.path.join(repo_path, file_info['name']), 'w') as f:
            f.write(file_info['content'])
            
    
    run_command(['git', 'add', '.'], repo_path)
    
    run_command(['git', 'commit', '--allow-empty', '-m', 'Apply updates for round 2'], repo_path)
    run_command(['git', 'push', 'origin', 'main'], repo_path)



def notify_evaluator(original_request, repo_url, commit_sha, pages_url):
    """Sends the result to the evaluation URL with retry logic."""
    payload = {
        "email": original_request['email'],
        "task": original_request['task'],
        "round": original_request['round'],
        "nonce": original_request['nonce'],
        "repo_url": repo_url,
        "commit_sha": commit_sha,
        "pages_url": pages_url,
    }
    
    url = original_request['evaluation_url']
    headers = {'Content-Type': 'application/json'}
    max_retries = 5
    delay = 1  

    for attempt in range(max_retries):
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=10)
            if response.status_code == 200:
                print(f"Successfully notified evaluation server at {url}")
                return
            else:
                print(f"Attempt {attempt + 1}: Failed to notify. Status: {response.status_code}, Body: {response.text}")
        except requests.exceptions.RequestException as e:
            print(f"Attempt {attempt + 1}: Error notifying server: {e}")

        time.sleep(delay)
        delay *= 2 
    
    raise Exception("Failed to notify evaluation server after multiple retries.")


if __name__ == '__main__':
    app.run(debug=True, port=5001)