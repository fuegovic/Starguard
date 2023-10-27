from flask import Flask, request, render_template, redirect, url_for
import os
from dotenv import load_dotenv
from authlib.integrations.flask_client import OAuth
from authlib.integrations.flask_client import OAuthError
from pymongo import MongoClient
from datetime import datetime, timezone

load_dotenv()

app = Flask('app')
app.template_folder = 'html'
app.secret_key = os.getenv('SECRET_KEY')
oauth = OAuth(app)

# Connect to the MongoDB server
try:
    client = MongoClient(host=os.getenv('MONGO_HOST'))
    db = client.get_database(os.getenv('MONGO_DATABASE'))
except Exception as e:
    print(f"Error connecting to MongoDB: {e}")
    raise


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
    redirect_uri = url_for('authorize', _external=True)
    return github.authorize_redirect(redirect_uri)

@app.route('/authorize')
def authorize():
    message = ""
    try:
        token = github.authorize_access_token()
        if token:
            # auth successful
            message = "Authentication successful!"
            print(f"Auth Status: {message}")

            user_resp = github.get('user', token=token)
            username = user_resp.json()['login']
            print(f"User's username: {username}")

            resp = github.get('user/emails', token=token)
            email = resp.json()[0]['email']
            print(f"User's email: {email}")
            print(f"token: {token}")

            # Save user information to MongoDB
            user_collection = db['users']

            # Check if a document with the same email exists
            existing_user = user_collection.find_one({'email': email})

            # Get the current timestamp in UTC
            current_time = datetime.now(timezone.utc).isoformat()

            user_data = {
                'username': username,
                'email': email,
                'token': token,
                'updated_at': current_time,
            }

            if existing_user:
                # Update the existing document
                user_collection.update_one({'email': email}, {'$set': user_data})
            else:
                # Insert a new document
                user_collection.insert_one(user_data)

        else:
            # auth failed
            message = "Invalid link!"
            print(f"Auth Status: {message}")

    except OAuthError as e:
        message = f"OAuth error: {e.description}"
        print(f"Auth Status: {message}")

    return render_template('result.html', message=message)

@app.route('/')
def home():
    return render_template('home.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
