from app import app, init_db

if __name__ == '__main__':
    print("Initializing database...")
    init_db()
    print("Database initialized successfully!")
    print("Starting Flask server...")
    print("Server running at: http://localhost:5000")
    print("Admin credentials: admin@shiptorockscollection.com / admin123")
    app.run(debug=True, host='0.0.0.0', port=5000)
