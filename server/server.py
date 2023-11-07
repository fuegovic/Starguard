# pylint: disable=missing-module-docstring
# pylint: disable=missing-class-docstring
# pylint: disable=missing-function-docstring
# pylint: disable=line-too-long

import os
from datetime import datetime, timezone
from flask import Flask, request, render_template, url_for, session
from dotenv import load_dotenv
from authlib.integrations.flask_client import OAuth
from authlib.integrations.flask_client import OAuthError
from pymongo import MongoClient
from werkzeug.middleware.proxy_fix import ProxyFix
import pymongo

load_dotenv()

owner = os.getenv('REPO_OWNER')
repo = os.getenv('GITHUB_REPO')
url = f"https://github.com/{owner}/{repo}/"

app = Flask(__name__, template_folder='./html')
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.template_folder = './html'
app.secret_key = os.getenv('SECRET_KEY')
oauth = OAuth(app)

CLIENT = None
DB = None

# Connect to the MongoDB server
try:
    CLIENT = MongoClient(host=os.getenv('MONGO_HOST'))
    DB = CLIENT.get_database(os.getenv('MONGO_DATABASE'))
except pymongo.errors.ConfigurationError as configuration_error:
    print(f"Error connecting to MongoDB: {configuration_error}")
except pymongo.errors.OperationFailure as operation_error:
    print(f"Error connecting to MongoDB: {operation_error}")
except pymongo.errors.ServerSelectionTimeoutError as timeout_error:
    print(f"Server selection timeout error: {timeout_error}")

github = oauth.register(
    name='github',
    client_id=os.getenv('GITHUB_CLIENT_ID'),
    client_secret=os.getenv('GITHUB_CLIENT_SECRET'),
    authorize_url='https://github.com/login/oauth/authorize',
    access_token_url='https://github.com/login/oauth/access_token',
    api_base_url='https://api.github.com/',
    client_kwargs={'scope': 'user:email repo'}
)

@app.route('/login')
def login():
    discord_username = request.args.get('name')
    discord_id = request.args.get('id')
    session['name'] = discord_username
    session['id'] = discord_id
    redirect_uri = url_for('authorize', _external=True)
    return github.authorize_redirect(redirect_uri)

@app.route('/authorize')
def authorize():
    discord_username = session.get('name')
    discord_id = session.get('id')
    message = ""
    user_data = {}

    try:
        token = github.authorize_access_token()
        if token:
            # Auth successful
            message = "Authentication successful!"
            print(f"Auth Status: {message}")

            user_resp = github.get('user', token=token)
            username = user_resp.json()['login']
            print(f"User's username: {username}")

            resp = github.get('user/emails', token=token)
            email = resp.json()[0]['email']
            print(f"User's email: {email}")
            print(f"Token: {token}")

            # Make a GET request to the "Check if a repository is starred" endpoint
            starred_resp = github.get(f'user/starred/{owner}/{repo}', token=token)
            print(f"starred response = {starred_resp}")

            # If the response status code is 204, the repository is starred
            starred_repo = starred_resp.status_code == 204

            # Get the current timestamp in UTC
            current_time = datetime.now(timezone.utc).isoformat()

            user_data = {
                'discord_username': discord_username,
                'discord_id': discord_id,
                'github_username': username,
                'github_email': email,
                'linked_repo': url,
                'starred_repo': starred_repo,
                'github_token': token,
                'updated_at': current_time,
            }

            save_user_data_to_db(user_data)

        else:
            # Auth failed
            message = "Invalid link!"
            print(f"Auth Status: {message}")

    except OAuthError as e:
        message = f"OAuth error: {e.description}"
        print(f"Auth Status: {message}")

    return render_template('result.html', message=message, user_data=user_data)

def save_user_data_to_db(user_data):
    # Save user information to MongoDB
    user_collection = DB['users']

    # Check if a document with the same email exists
    existing_user = user_collection.find_one({'github_email': user_data['github_email']})

    if existing_user:
        # Update the existing document
        user_collection.update_one({'github_email': user_data['github_email']}, {'$set': user_data})
    else:
        # Insert a new document
        user_collection.insert_one(user_data)

@app.route('/')
def home():
    return render_template('home.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=os.getenv('SERVER_PORT'), debug=False)
