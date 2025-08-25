from app import app
from flask_cors import CORS
import firebase_admin
from firebase_admin import credentials, firestore

# Initialize Firebase
cred = credentials.Certificate("credentials.json")  # Path to your Firebase credentials file
firebase_admin.initialize_app(cred)

# Get Firestore client (database)
db = firestore.client()

if __name__ == '__main__':
    # Enable CORS for Next.js frontend
    CORS(
        app,
        supports_credentials=True,
        origins=["http://localhost:3000"],   # Your frontend URL
        allow_headers=["Content-Type", "Authorization"],
        methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"]
    )

    print("Firebase initialized successfully!")
    print("Server running at: http://localhost:5000")
    print("Admin credentials: admin@shiptorockscollection.com / admin123")

    app.run(debug=True, host='0.0.0.0', port=5000)
