from ghapi.all import GhApi
import re
import os
import config

# GitHub issue URL pattern
GITHUB_ISSUE_URL_PATTERN = re.compile(r'github\.com\/(.*?)\/(.*?)\/issues\/(\d+)')

def is_from_github_url(data_path: str):
    """Check if the data path is a GitHub issue URL."""
    return GITHUB_ISSUE_URL_PATTERN.search(data_path) is not None

def get_github_issue(api: GhApi, issue_url: str):
    """Fetch a GitHub issue given its URL."""
    match = GITHUB_ISSUE_URL_PATTERN.search(issue_url)
    if match:
        owner, repo, issue_number = match.groups()
        issue = api.issues.get(owner, repo, int(issue_number))
        return issue
    else:
        raise ValueError("Invalid GitHub issue URL")

# Example usage
if __name__ == "__main__":
    # Initialize the GitHub API client with your token
    cfg = config.Config(os.path.join(os.getcwd(), "keys.cfg"))
    api = GhApi(token=cfg.get("GITHUB_TOKEN", "token"))

    # Example GitHub issue URL
    issue_url = "https://github.com/Hausable-Cosmos/Cosmos-FrontEnd/issues/71"

    if is_from_github_url(issue_url):
        issue = get_github_issue(api, issue_url)
        print(f"Issue Title: {issue.title}")
        print(f"Issue Body: {issue.body}")
    else:
        print("The provided URL is not a valid GitHub issue URL.")