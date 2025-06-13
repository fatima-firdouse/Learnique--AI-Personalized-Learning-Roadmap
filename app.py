from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, abort
from werkzeug.security import generate_password_hash, check_password_hash
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
import os
import json
from datetime import datetime, timedelta, date
from dateutil.relativedelta import relativedelta
from collections import defaultdict
import logging


# ---- Flask app setup ----
app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), 'templates'),
    static_folder=os.path.join(os.path.dirname(__file__), 'static')
)
app.config['SECRET_KEY'] = '018d614ec22d30005a9431ee637eee0f335d55dab894b7febde2767ead8b5b72'
logging.basicConfig(level=logging.INFO)

basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'data', 'roadmaps.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)


PROGRESS_FILE = os.path.join(basedir, 'progress_data.json')
ROADMAPS_DIR = os.path.join(basedir, 'templates', 'roadmaps')

app.config['UPLOAD_FOLDER'] = os.path.join(app.static_folder, 'profile_images')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024  # 2MB limit

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

os.makedirs(os.path.join(basedir, 'data'), exist_ok=True)
os.makedirs(ROADMAPS_DIR, exist_ok=True)
os.makedirs(os.path.join(basedir, 'data', 'roadmaps'), exist_ok=True)

# ------------------- Database Models -------------------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    last_login = db.Column(db.DateTime)
    login_count = db.Column(db.Integer, default=0)
    phone_number = db.Column(db.String(20), nullable=True)
    profile_image = db.Column(db.String(255), nullable=True)
    notifications = db.Column(db.Boolean, default=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class UserRoadmap(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    role = db.Column(db.String(50), nullable=False)
    target_duration_weeks = db.Column(db.Integer, nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    target_completion_date = db.Column(db.Date, nullable=False)
    current_streak = db.Column(db.Integer, default=0)
    longest_streak = db.Column(db.Integer, default=0)
    last_activity_date = db.Column(db.Date)
    user = db.relationship('User', backref=db.backref('roadmaps', lazy=True))

class RoadmapItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    roadmap_id = db.Column(db.Integer, db.ForeignKey('user_roadmap.id'), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    duration_days = db.Column(db.Integer, nullable=False)
    sequence_order = db.Column(db.Integer, nullable=False)
    is_completed = db.Column(db.Boolean, default=False)
    completed_date = db.Column(db.Date)
    module_name = db.Column(db.String(200))
    step_code = db.Column(db.String(50))
    roadmap = db.relationship('UserRoadmap', backref=db.backref('items', lazy=True))

class UserSession(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    start_time = db.Column(db.DateTime, nullable=False)
    end_time = db.Column(db.DateTime)
    user = db.relationship('User', backref=db.backref('sessions', lazy=True))

def get_current_user():
    if 'username' not in session:
        return None
    return User.query.filter_by(username=session['username']).first()

def load_progress(username):
    if not os.path.exists(PROGRESS_FILE):
        return {}
    with open(PROGRESS_FILE, 'r') as f:
        all_progress = json.load(f)
        return all_progress.get(username, {})

def save_progress(username, progress_data):
    all_progress = {}
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, 'r') as f:
            all_progress = json.load(f)
    all_progress[username] = progress_data
    with open(PROGRESS_FILE, 'w') as f:
        json.dump(all_progress, f)

def sanitize_rolename(role):
    if isinstance(role, str):
        return role.lower().replace(' ', '_').replace('&', 'and')
    return role

def get_roadmap_filename(role):
    sanitized = sanitize_rolename(role)
    return f"{sanitized}.html"

def compute_progress(roadmap):
    total = len(roadmap.items)
    if total == 0:
        return 0
    completed = sum(1 for i in roadmap.items if i.is_completed)
    return int(completed / total * 100)

def compute_overall_progress(user_courses):
    if not user_courses:
        return 0
    return int(sum(c['progress'] for c in user_courses) / len(user_courses))

# ------------------- UNIVERSAL ROADMAP ROUTE -------------------
@app.route('/<role>-roadmap')
def serve_roadmap(role):
    user = get_current_user()
    if not user:
        flash('Please login first', 'error')
        return redirect(url_for('login_register'))

    json_path = os.path.join(basedir, 'data', 'roadmaps', f"{role.lower()}.json")
    if not os.path.exists(json_path):
        abort(404, description=f"Roadmap data not found for {role}.")

    with open(json_path, 'r', encoding='utf-8') as f:
        roadmap_data = json.load(f)
    modules = roadmap_data['modules']
    projects = roadmap_data.get('projects', [])

    start_date = request.args.get('start')
    end_date = request.args.get('end')

    user_roadmap = UserRoadmap.query.filter_by(user_id=user.id, role=role).order_by(UserRoadmap.id.desc()).first()
    if not start_date or not end_date:
        if user_roadmap:
            return redirect(
                url_for('serve_roadmap', role=role,
                        start=user_roadmap.start_date.strftime('%Y-%m-%d'),
                        end=user_roadmap.target_completion_date.strftime('%Y-%m-%d'))
            )
        else:
            flash('Please select your start and end dates for this roadmap.', 'error')
            return redirect(url_for('welcome'))

    try:
        start_date_obj = datetime.strptime(start_date, '%Y-%m-%d').date() if start_date else None
        end_date_obj = datetime.strptime(end_date, '%Y-%m-%d').date() if end_date else None
    except ValueError:
        abort(400, description="Invalid date format. Use YYYY-MM-DD")

    display_role = ' '.join(word.capitalize() for word in role.replace('_', ' ').replace('-', ' ').split())

    # Prepare step_code to db id mapping for use in template/JS
    roadmap_id = user_roadmap.id if user_roadmap else None
    step_id_map = {}
    if roadmap_id:
        db_items = RoadmapItem.query.filter_by(roadmap_id=roadmap_id).all()
        for item in db_items:
            step_id_map[item.step_code] = item.id

    return render_template(
        'roadmaps/generic_roadmap.html',
        role=display_role,
        start_date=start_date_obj,
        end_date=end_date_obj,
        modules=modules,
        projects=projects,
        username=user.username,
        step_id_map=step_id_map
    )

@app.route('/generate-roadmap', methods=['POST'])
def generate_roadmap():
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.get_json()
    role = data.get('role', 'SQL')
    start_date = data.get('start')
    end_date = data.get('end')

    if not all([role, start_date, end_date]):
        return jsonify({'error': 'Missing required fields'}), 400

    try:
        template_path = os.path.join(basedir, 'data', 'roadmaps', f"{role.lower()}.json")
        if not os.path.exists(template_path):
            return jsonify({'error': f'Roadmap not found for {role}'}), 404

        roadmap = UserRoadmap(
            user_id=user.id,
            role=role,
            start_date=datetime.strptime(start_date, '%Y-%m-%d').date(),
            target_completion_date=datetime.strptime(end_date, '%Y-%m-%d').date(),
            target_duration_weeks=(datetime.strptime(end_date, '%Y-%m-%d').date() -
                                   datetime.strptime(start_date, '%Y-%m-%d').date()).days // 7
        )
        db.session.add(roadmap)
        db.session.commit()

        with open(template_path, 'r', encoding='utf-8') as f:
            roadmap_data = json.load(f)

        sequence_order = 1
        for module in roadmap_data['modules']:
            module_title = module['title']
            for step in module['steps']:
                roadmap_item = RoadmapItem(
                    roadmap_id=roadmap.id,
                    title=step['title'],
                    description=module_title,
                    duration_days=step.get('duration_days', 1),
                    sequence_order=sequence_order,
                    module_name=module_title,
                    step_code=step.get('id', '')
                )
                db.session.add(roadmap_item)
                sequence_order += 1

        db.session.commit()

        return jsonify({
            'redirect': url_for('serve_roadmap',
                                role=role.lower().replace(' ', '-'),
                                start=start_date,
                                end=end_date)
        })

    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500
@app.route('/save_progress', methods=['POST'])
def save_progress_api():
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.get_json()
    item_id = data.get('item')
    checked = data.get('checked', False)
    today = date.today().isoformat()

    progress = load_progress(user.username)
    progress.setdefault('steps', {})
    progress['steps'][item_id] = {
        'checked': checked,
        'date': today if checked else None
    }

    # Try to find by integer id (old way)
    roadmap_item = RoadmapItem.query.join(UserRoadmap).filter(
        RoadmapItem.id == item_id,
        UserRoadmap.user_id == user.id
    ).first()

    # If not found, try by step_code (step id in JSON, without course prefix)
    if not roadmap_item:
        # Remove course prefix (everything up to the first '-')
        if '-' in item_id:
            step_code = item_id.split('-', 1)[1]  # e.g., 'frontend-step1-1' → 'step1-1'
        else:
            step_code = item_id
        roadmap_item = RoadmapItem.query.join(UserRoadmap).filter(
            RoadmapItem.step_code == step_code,
            UserRoadmap.user_id == user.id
        ).first()

    if roadmap_item:
        roadmap_item.is_completed = checked
        roadmap_item.completed_date = date.today() if checked else None
        db.session.commit()

    if checked:
        last_streak_date = progress.get('last_streak_date')
        if last_streak_date != today:
            if last_streak_date and (date.today() - datetime.strptime(last_streak_date, '%Y-%m-%d').date()).days == 1:
                progress['streak'] = progress.get('streak', 0) + 1
            else:
                progress['streak'] = 1
            progress['last_streak_date'] = today

    save_progress(user.username, progress)
    return jsonify({'status': 'success', 'streak': progress.get('streak', 0)})

@app.route('/get_progress')
def get_progress_api():
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
    item_id = request.args.get('item')
    progress = load_progress(user.username)
    checked = progress.get('steps', {}).get(item_id, {}).get('checked', False)
    return jsonify({'checked': checked})

@app.route('/api/save-progress', methods=['POST'])
def api_save_progress():
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json()
    save_progress(user.username, data)
    return jsonify({'status': 'success'})

@app.route('/api/load-progress')
def api_load_progress():
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
    progress = load_progress(user.username)
    return jsonify(progress)

@app.route('/')
def home():
    if 'username' in session:
        return redirect(url_for('interests'))
    return redirect(url_for('login_register'))

@app.route('/welcome')
def welcome():
    user = get_current_user()
    if not user:
        flash('Please login first', 'error')
        return redirect(url_for('login_register'))
    return render_template('welcome.html', username=user.username)

@app.route('/interests')
def interests():
    user = get_current_user()
    if not user:
        flash('Please login first', 'error')
        return redirect(url_for('login_register'))

    roles = [
        "Frontend", "Backend", "DevOps", "Full_Stack", "AI_Engineer", "Data_Analyst", "AI_Data_Scientist", "Android", "iOS", "PostgreSQL",
        "Blockchain", "Software_Architect", "Cyber_Security", "UX_Design", "Game_Developer", "Technical_Writer", "MLOps", "Product_Manager",
        "SQL", "React", "Python", "JavaScript", "AWS", "Docker", "Git&Github", "Node.js", "Typescript", "Kubernetes", "Flutter", "DSA",
        "Linux", "Prompt_Engineering", "Terraform", "PHP", "Cloudflare"
    ]
    card_links = {}
    for role in roles:
        user_roadmap = UserRoadmap.query.filter_by(user_id=user.id, role=role).order_by(UserRoadmap.id.desc()).first()
        if user_roadmap:
            link = url_for('serve_roadmap',
                           role=role.lower().replace(' ', '-'),
                           start=user_roadmap.start_date.strftime('%Y-%m-%d'),
                           end=user_roadmap.target_completion_date.strftime('%Y-%m-%d'))
        else:
            link = url_for('index', role=role)
        card_links[role] = link

    return render_template('interests.html', card_links=card_links)

@app.route('/login-register', methods=['GET', 'POST'])
def login_register():
    return render_template('login.html')

@app.route('/login', methods=['POST'])
def login():
    username = request.form.get('username')
    password = request.form.get('password')
    user = User.query.filter_by(username=username).first()
    if user and user.check_password(password):
        user.last_login = datetime.utcnow()
        user.login_count = (user.login_count or 0) + 1
        db.session.commit()
        session['username'] = username
        session_obj = UserSession(user_id=user.id, start_time=datetime.utcnow())
        db.session.add(session_obj)
        db.session.commit()
        session['session_id'] = session_obj.id
        flash('Login successful!', 'success')
        return redirect(url_for('interests'))
    flash('Invalid username or password', 'error')
    return redirect(url_for('login_register'))

@app.route('/register', methods=['POST'])
def register():
    username = request.form.get('username')
    email = request.form.get('email')
    password = request.form.get('password')
    if User.query.filter_by(username=username).first():
        flash('Username already exists', 'error')
        return redirect(url_for('login_register', _anchor='register'))
    if User.query.filter_by(email=email).first():
        flash('Email already registered', 'error')
        return redirect(url_for('login_register', _anchor='register'))
    user = User(username=username, email=email)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    session['username'] = username
    flash('Registration successful! Please login', 'success')
    return redirect(url_for('login_register', _anchor='login'))

@app.route('/logout')
def logout():
    if 'session_id' in session:
        session_obj = UserSession.query.filter_by(id=session['session_id']).first()
        if session_obj and not session_obj.end_time:
            session_obj.end_time = datetime.utcnow()
            db.session.commit()
        session.pop('session_id', None)
    session.pop('username', None)
    flash('You have been logged out', 'success')
    return redirect(url_for('login_register'))

@app.route('/index')
def index():
    role = request.args.get('role', 'Frontend')

    user = get_current_user()
    if not user:
        return render_template('index.html', selected_role=role)

    user_roadmap = UserRoadmap.query.filter_by(user_id=user.id, role=role).order_by(UserRoadmap.id.desc()).first()

    if user_roadmap:
        return redirect(url_for(
            'serve_roadmap',
            role=role.lower().replace(' ', '-'),
            start=user_roadmap.start_date.strftime('%Y-%m-%d'),
            end=user_roadmap.target_completion_date.strftime('%Y-%m-%d')
        ))

    return render_template('index.html', selected_role=role)

@app.route('/profile')
def profile():
    if 'username' not in session:
        flash('Please login first', 'error')
        return redirect(url_for('login_register'))

    user = User.query.filter_by(username=session['username']).first()
    if not user:
        flash('User not found', 'error')
        return redirect(url_for('login_register'))

    return render_template('profile.html', user=user)

today = date.today()





@app.route('/activity')
def activity():
    user = get_current_user()
    if not user:
        flash('Please login first', 'error')
        return redirect(url_for('login_register'))

    today = date.today()
    user_sessions = UserSession.query.filter_by(user_id=user.id).all()
    session_times = []
    for s in user_sessions:
        if s.end_time:
            duration = (s.end_time - s.start_time).total_seconds()
        else:
            duration = (datetime.utcnow() - s.start_time).total_seconds()
        session_times.append((s.start_time.date(), duration))

    daily_time = defaultdict(int)
    for dt, seconds in session_times:
        daily_time[dt] += seconds

    day_labels, day_data = [], []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        day_labels.append(d.strftime("%d"))
        day_data.append(int(daily_time.get(d, 0)) // 60)

    week_time = defaultdict(int)
    for dt, seconds in session_times:
        week_start = dt - timedelta(days=dt.weekday())
        week_time[week_start] += seconds
    week_labels, week_data = [], []
    for i in range(6, -1, -1):
        week_start = today - timedelta(days=today.weekday() + 7 * i)
        week_labels.append(week_start.strftime("%b %d"))
        week_data.append(int(week_time.get(week_start, 0)) // 60)

    month_time = defaultdict(int)
    for dt, seconds in session_times:
        month_start = dt.replace(day=1)
        month_time[month_start] += seconds
    month_labels, month_data = [], []
    for i in range(6, -1, -1):
        month_start = (today.replace(day=1) - relativedelta(months=i))
        month_lbl = month_start.strftime("%b")
        month_labels.append(month_lbl)
        month_data.append(int(month_time.get(month_start, 0)) // 60)

    # --- Course Progress Calculation ---
    roadmaps = UserRoadmap.query.filter_by(user_id=user.id).all()
    seen_roles = {}
    for roadmap in sorted(roadmaps, key=lambda x: x.start_date, reverse=True):
        if roadmap.role not in seen_roles and roadmap.start_date and roadmap.target_completion_date:
            seen_roles[roadmap.role] = roadmap

    user_courses = []
    completed_count = 0
    in_progress_count = 0
    for roadmap in seen_roles.values():
        total = len(roadmap.items)
        completed = sum(1 for i in roadmap.items if i.is_completed)
        progress = int(completed / total * 100) if total else 0
        course_info = {
            'name': roadmap.role.replace('_', ' ').replace('-', ' ').title(),
            'icon': 'star',
            'icon_bg': '#ffe9a7',
            'chapters': total,
            'enrolled_date': roadmap.start_date.strftime('%Y-%m-%d'),
            'progress': progress
        }
        user_courses.append(course_info)
        if total > 0 and completed == total:
            completed_count += 1
        elif total > 0:
            in_progress_count += 1

    user_progress = compute_overall_progress(user_courses)

    # --- Show correct streak based on progress file ---
    progress = load_progress(user.username)
    streak = progress.get('streak', 0)
    last_streak_date = progress.get('last_streak_date')
    if last_streak_date:
        try:
            last_streak_date_dt = datetime.strptime(last_streak_date, '%Y-%m-%d').date()
            if (today - last_streak_date_dt).days == 1:
                streak += 1
            elif (today - last_streak_date_dt).days > 1:
                streak = 1
        except ValueError:
            pass
    user_streak = streak

    last_week_progress = 0
    if len(week_data) >= 2 and sum(week_data) > 0:
        last_week_progress = int((week_data[-2] / max(1, sum(week_data))) * 100)
    else:
        last_week_progress = 0

    # For donut chart: only Completed & In Progress
    total_courses = completed_count + in_progress_count
    completed_percent = int(completed_count / total_courses * 100) if total_courses else 0
    in_progress_percent = 100 - completed_percent if total_courses else 0

    return render_template(
        'activity.html',
        today=today,
        username=user.username,
        streak=user_streak,           # for backwards compatibility in template
        user_streak=user_streak,      # for "streak-number" in template
        user_courses=user_courses,
        user_progress=user_progress,
        day_data=day_data,
        day_labels=day_labels,
        week_data=week_data,
        week_labels=week_labels,
        month_data=month_data,
        month_labels=month_labels,
        last_week_progress=last_week_progress,
        # Donut chart variables:
        completed_courses=completed_count,
        in_progress_courses=in_progress_count,
        completed_percent=completed_percent,
        in_progress_percent=in_progress_percent
    )

@app.route('/edit-profile', methods=['GET', 'POST'])
def edit_profile():
    if 'username' not in session:
        flash('Please login first', 'error')
        return redirect(url_for('login_register'))
    user = User.query.filter_by(username=session['username']).first()
    if request.method == 'POST':
        old_username = user.username
        new_username = request.form.get('username')
        if new_username != old_username:
            if User.query.filter_by(username=new_username).first():
                flash('Username already taken by another user.', 'error')
                return redirect(url_for('edit_profile'))
        user.username = new_username
        user.email = request.form.get('email')
        user.phone_number = request.form.get('phone_number')
        user.notifications = bool(request.form.get('notifications'))
        file = request.files.get('profile_image')
        if file and allowed_file(file.filename):
            filename = secure_filename(f"{user.username}_{file.filename}")
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            user.profile_image = f"profile_images/{filename}"
        db.session.commit()
        session['username'] = user.username
        flash('Profile updated successfully!', 'success')
        return redirect(url_for('profile'))
    return render_template('edit_profile.html', user=user)

@app.route('/change-password', methods=['GET', 'POST'])
def change_password():
    if 'username' not in session:
        flash('Please login first', 'error')
        return redirect(url_for('login_register'))
    user = User.query.filter_by(username=session['username']).first()
    if request.method == 'POST':
        old_password = request.form.get('old_password')
        new_password = request.form.get('new_password')
        if not user.check_password(old_password):
            flash('Old password incorrect', 'error')
        elif not new_password or len(new_password) < 6:
            flash('New password must be at least 6 characters.', 'error')
        else:
            user.set_password(new_password)
            db.session.commit()
            flash('Password updated successfully!', 'success')
            return redirect(url_for('profile'))
    return render_template('change_password.html')

@app.route('/settings')
def settings():
    if 'username' not in session:
        flash('Please login first', 'error')
        return redirect(url_for('login_register'))
    return render_template('settings.html')

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)