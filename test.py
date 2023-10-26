import requests

github_token = ""
repo_owner = ""
repo_name = ""

headers = {
    "Accept": "application/vnd.github.v3+json",
    "Authorization": f"Bearer {github_token}"
}

# Check if the repository exists and if your token has permission to star
repository_exists = True  # Replace with your repository existence check logic
has_permission_to_star = True  # Replace with your permission check logic

if repository_exists and has_permission_to_star:
    # Star the repository
    star_url = f"https://api.github.com/user/starred/{repo_owner}/{repo_name}"
    response = requests.put(star_url, headers=headers)

    if response.status_code == 204:
        print(f"The repository {repo_owner}/{repo_name} was successfully starred!")
    else:
        print(f"An error occurred while starring the repository: {response.text}")
else:
    print("The repository does not exist or you do not have the necessary permission to star it.")