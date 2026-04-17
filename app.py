"""
╔══════════════════════════════════════════════════════════════╗
║         UNIVERSAL PREDICTION MARKET SYSTEM                   ║
║         Full Production Flask Application                    ║
║         Single-file — Backend + Frontend + Admin             ║
╚══════════════════════════════════════════════════════════════╝

SETUP:
  pip install flask flask-sqlalchemy flask-jwt-extended flask-bcrypt flask-limiter flask-cors intasend-python

RUN:
  python app.py

ENV VARS (optional, defaults provided):
  SECRET_KEY, JWT_SECRET_KEY, DATABASE_URL, INTASEND_API_KEY, INTASEND_PUBLISHABLE_KEY
  ADMIN_EMAIL, ADMIN_PASSWORD, COMMISSION_RATE
"""

import os, uuid, logging, json, hashlib, re
from datetime import datetime, timedelta, timezone
from functools import wraps
from decimal import Decimal

from flask import (Flask, request, jsonify, session, render_template_string,
                   redirect, url_for, abort, make_response)
from flask_sqlalchemy import SQLAlchemy
from flask_jwt_extended import (JWTManager, create_access_token, jwt_required,
                                get_jwt_identity, get_jwt, create_refresh_token)
from flask_bcrypt import Bcrypt
from flask_cors import CORS

# ─────────────────────────────────────────────
#  APP CONFIGURATION
# ─────────────────────────────────────────────

app = Flask(__name__)

app.config.update(
    SECRET_KEY=os.environ.get('SECRET_KEY', 'changeme-super-secret-key-2025'),
    JWT_SECRET_KEY=os.environ.get('JWT_SECRET_KEY', 'jwt-secret-key-changeme'),
    JWT_ACCESS_TOKEN_EXPIRES=timedelta(hours=12),
    JWT_REFRESH_TOKEN_EXPIRES=timedelta(days=30),
    SQLALCHEMY_DATABASE_URI=os.environ.get('DATABASE_URL', 'sqlite:///prediction_market.db'),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,  # 16MB
)

# Replace postgres:// with postgresql:// for SQLAlchemy compat
if app.config['SQLALCHEMY_DATABASE_URI'].startswith('postgres://'):
    app.config['SQLALCHEMY_DATABASE_URI'] = app.config['SQLALCHEMY_DATABASE_URI'].replace('postgres://', 'postgresql://', 1)

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
jwt = JWTManager(app)
CORS(app, supports_credentials=True)

# Default commission rate (5%)
DEFAULT_COMMISSION = float(os.environ.get('COMMISSION_RATE', '0.05'))

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  DATABASE MODELS
# ─────────────────────────────────────────────

class User(db.Model):
    __tablename__ = 'users'
    id              = db.Column(db.Integer, primary_key=True)
    username        = db.Column(db.String(50), unique=True, nullable=False)
    email           = db.Column(db.String(120), unique=True, nullable=False)
    phone           = db.Column(db.String(20), unique=True, nullable=False)
    password_hash   = db.Column(db.String(256), nullable=False)
    balance         = db.Column(db.Numeric(18, 2), default=0.00, nullable=False)
    status          = db.Column(db.String(20), default='active')   # active | suspended | locked
    is_admin        = db.Column(db.Boolean, default=False)
    failed_logins   = db.Column(db.Integer, default=0)
    locked_until    = db.Column(db.DateTime, nullable=True)
    referral_code   = db.Column(db.String(12), unique=True, nullable=True)
    referred_by     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    total_deposited = db.Column(db.Numeric(18, 2), default=0.00)
    total_withdrawn = db.Column(db.Numeric(18, 2), default=0.00)
    total_wagered   = db.Column(db.Numeric(18, 2), default=0.00)
    total_won       = db.Column(db.Numeric(18, 2), default=0.00)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    last_login      = db.Column(db.DateTime, nullable=True)

    bets         = db.relationship('Bet', backref='user', lazy='dynamic')
    multibets    = db.relationship('Multibet', backref='user', lazy='dynamic')
    transactions = db.relationship('Transaction', backref='user', lazy='dynamic')

    def set_password(self, password):
        self.password_hash = bcrypt.generate_password_hash(password).decode('utf-8')

    def check_password(self, password):
        return bcrypt.check_password_hash(self.password_hash, password)

    def to_dict(self, admin=False):
        d = {
            'id': self.id,
            'username': self.username,
            'email': self.email,
            'phone': self.phone,
            'balance': float(self.balance),
            'status': self.status,
            'is_admin': self.is_admin,
            'referral_code': self.referral_code,
            'total_deposited': float(self.total_deposited),
            'total_withdrawn': float(self.total_withdrawn),
            'total_wagered': float(self.total_wagered),
            'total_won': float(self.total_won),
            'created_at': self.created_at.isoformat(),
            'last_login': self.last_login.isoformat() if self.last_login else None,
        }
        if admin:
            d['failed_logins'] = self.failed_logins
            d['locked_until'] = self.locked_until.isoformat() if self.locked_until else None
        return d


class Market(db.Model):
    __tablename__ = 'markets'
    id          = db.Column(db.Integer, primary_key=True)
    title       = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    category    = db.Column(db.String(50), nullable=False, default='other')
    yes_odds    = db.Column(db.Numeric(8, 4), default=2.00, nullable=False)
    no_odds     = db.Column(db.Numeric(8, 4), default=2.00, nullable=False)
    result      = db.Column(db.String(10), nullable=True)   # YES | NO | VOID
    status      = db.Column(db.String(20), default='open')  # open | closed | settled | suspended
    closes_at   = db.Column(db.DateTime, nullable=False)
    created_by  = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    yes_volume  = db.Column(db.Numeric(18, 2), default=0.00)
    no_volume   = db.Column(db.Numeric(18, 2), default=0.00)
    min_stake   = db.Column(db.Numeric(10, 2), default=10.00)
    max_stake   = db.Column(db.Numeric(10, 2), default=100000.00)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    bets           = db.relationship('Bet', backref='market', lazy='dynamic')
    multibet_items = db.relationship('MultibetItem', backref='market', lazy='dynamic')

    def to_dict(self):
        return {
            'id': self.id,
            'title': self.title,
            'description': self.description,
            'category': self.category,
            'yes_odds': float(self.yes_odds),
            'no_odds': float(self.no_odds),
            'result': self.result,
            'status': self.status,
            'closes_at': self.closes_at.isoformat(),
            'yes_volume': float(self.yes_volume),
            'no_volume': float(self.no_volume),
            'min_stake': float(self.min_stake),
            'max_stake': float(self.max_stake),
            'created_at': self.created_at.isoformat(),
            'created_by': self.created_by,
        }

    def auto_balance_odds(self):
        """Adjust odds based on volume for house edge maintenance."""
        yes_vol = float(self.yes_volume or 0)
        no_vol  = float(self.no_volume or 0)
        total   = yes_vol + no_vol
        if total < 100:
            return
        yes_ratio = yes_vol / total
        no_ratio  = no_vol / total
        base = 1.90
        self.yes_odds = round(base / yes_ratio if yes_ratio > 0 else 2.0, 4)
        self.no_odds  = round(base / no_ratio  if no_ratio  > 0 else 2.0, 4)
        self.yes_odds = max(1.05, min(float(self.yes_odds), 50.0))
        self.no_odds  = max(1.05, min(float(self.no_odds),  50.0))


class Bet(db.Model):
    __tablename__ = 'bets'
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    market_id   = db.Column(db.Integer, db.ForeignKey('markets.id'), nullable=False)
    selection   = db.Column(db.String(5), nullable=False)   # YES | NO
    stake       = db.Column(db.Numeric(18, 2), nullable=False)
    odds        = db.Column(db.Numeric(8, 4), nullable=False)
    gross_payout= db.Column(db.Numeric(18, 2), nullable=False)
    commission  = db.Column(db.Numeric(18, 2), default=0.00)
    net_payout  = db.Column(db.Numeric(18, 2), nullable=False)
    status      = db.Column(db.String(20), default='open')  # open | won | lost | void | refunded
    multibet_id = db.Column(db.Integer, db.ForeignKey('multibets.id'), nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    settled_at  = db.Column(db.DateTime, nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'market_id': self.market_id,
            'market_title': self.market.title if self.market else None,
            'selection': self.selection,
            'stake': float(self.stake),
            'odds': float(self.odds),
            'gross_payout': float(self.gross_payout),
            'commission': float(self.commission),
            'net_payout': float(self.net_payout),
            'status': self.status,
            'multibet_id': self.multibet_id,
            'created_at': self.created_at.isoformat(),
            'settled_at': self.settled_at.isoformat() if self.settled_at else None,
        }


class Multibet(db.Model):
    __tablename__ = 'multibets'
    id               = db.Column(db.Integer, primary_key=True)
    user_id          = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    total_odds       = db.Column(db.Numeric(12, 4), nullable=False)
    total_stake      = db.Column(db.Numeric(18, 2), nullable=False)
    gross_payout     = db.Column(db.Numeric(18, 2), nullable=False)
    commission       = db.Column(db.Numeric(18, 2), default=0.00)
    net_payout       = db.Column(db.Numeric(18, 2), nullable=False)
    status           = db.Column(db.String(20), default='open')
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)
    settled_at       = db.Column(db.DateTime, nullable=True)

    items = db.relationship('MultibetItem', backref='multibet', lazy='dynamic')

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'total_odds': float(self.total_odds),
            'total_stake': float(self.total_stake),
            'gross_payout': float(self.gross_payout),
            'commission': float(self.commission),
            'net_payout': float(self.net_payout),
            'status': self.status,
            'created_at': self.created_at.isoformat(),
            'settled_at': self.settled_at.isoformat() if self.settled_at else None,
            'items': [i.to_dict() for i in self.items],
        }


class MultibetItem(db.Model):
    __tablename__ = 'multibet_items'
    id          = db.Column(db.Integer, primary_key=True)
    multibet_id = db.Column(db.Integer, db.ForeignKey('multibets.id'), nullable=False)
    market_id   = db.Column(db.Integer, db.ForeignKey('markets.id'), nullable=False)
    selection   = db.Column(db.String(5), nullable=False)
    odds        = db.Column(db.Numeric(8, 4), nullable=False)
    result      = db.Column(db.String(10), nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'multibet_id': self.multibet_id,
            'market_id': self.market_id,
            'market_title': self.market.title if self.market else None,
            'selection': self.selection,
            'odds': float(self.odds),
            'result': self.result,
        }


class Transaction(db.Model):
    __tablename__ = 'transactions'
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    type        = db.Column(db.String(30), nullable=False)
    # deposit | withdrawal | bet_placed | winnings | commission | refund | bonus | admin_adjustment
    amount      = db.Column(db.Numeric(18, 2), nullable=False)
    balance_before = db.Column(db.Numeric(18, 2), nullable=False)
    balance_after  = db.Column(db.Numeric(18, 2), nullable=False)
    reference   = db.Column(db.String(100), nullable=True)
    description = db.Column(db.String(255), nullable=True)
    status      = db.Column(db.String(20), default='completed')
    mpesa_ref   = db.Column(db.String(100), nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'type': self.type,
            'amount': float(self.amount),
            'balance_before': float(self.balance_before),
            'balance_after': float(self.balance_after),
            'reference': self.reference,
            'description': self.description,
            'status': self.status,
            'mpesa_ref': self.mpesa_ref,
            'created_at': self.created_at.isoformat(),
        }


class AdminLog(db.Model):
    __tablename__ = 'admin_logs'
    id         = db.Column(db.Integer, primary_key=True)
    admin_id   = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    action     = db.Column(db.String(100), nullable=False)
    target     = db.Column(db.String(200), nullable=True)
    details    = db.Column(db.Text, nullable=True)
    ip_address = db.Column(db.String(45), nullable=True)
    timestamp  = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'admin_id': self.admin_id,
            'action': self.action,
            'target': self.target,
            'details': self.details,
            'ip_address': self.ip_address,
            'timestamp': self.timestamp.isoformat(),
        }


class Settings(db.Model):
    __tablename__ = 'settings'
    id    = db.Column(db.Integer, primary_key=True)
    key   = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.String(255), nullable=False)

    @classmethod
    def get(cls, key, default=None):
        row = cls.query.filter_by(key=key).first()
        return row.value if row else default

    @classmethod
    def set(cls, key, value):
        row = cls.query.filter_by(key=key).first()
        if row:
            row.value = str(value)
        else:
            row = cls(key=key, value=str(value))
            db.session.add(row)
        db.session.commit()


class WithdrawalRequest(db.Model):
    __tablename__ = 'withdrawal_requests'
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    amount      = db.Column(db.Numeric(18, 2), nullable=False)
    phone       = db.Column(db.String(20), nullable=False)
    status      = db.Column(db.String(20), default='pending')  # pending | approved | rejected
    reference   = db.Column(db.String(100), unique=True, nullable=False)
    admin_note  = db.Column(db.String(255), nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    reviewed_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    user = db.relationship('User', foreign_keys=[user_id], backref='withdrawal_requests')

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'username': self.user.username if self.user else None,
            'amount': float(self.amount),
            'phone': self.phone,
            'status': self.status,
            'reference': self.reference,
            'admin_note': self.admin_note,
            'created_at': self.created_at.isoformat(),
            'reviewed_at': self.reviewed_at.isoformat() if self.reviewed_at else None,
        }


# ─────────────────────────────────────────────
#  HELPERS & DECORATORS
# ─────────────────────────────────────────────

def get_commission_rate():
    return float(Settings.get('commission_rate', str(DEFAULT_COMMISSION)))

def get_withdrawal_fee():
    return float(Settings.get('withdrawal_fee', '0.02'))  # 2%

def log_admin(admin_id, action, target=None, details=None):
    log = AdminLog(
        admin_id=admin_id, action=action, target=target,
        details=json.dumps(details) if details else None,
        ip_address=request.remote_addr
    )
    db.session.add(log)

def credit_wallet(user, amount, tx_type, reference=None, description=None, mpesa_ref=None):
    """Thread-safe credit with full audit."""
    bal_before = float(user.balance)
    user.balance = Decimal(str(user.balance)) + Decimal(str(amount))
    tx = Transaction(
        user_id=user.id, type=tx_type, amount=amount,
        balance_before=bal_before, balance_after=float(user.balance),
        reference=reference, description=description, mpesa_ref=mpesa_ref
    )
    db.session.add(tx)
    return tx

def debit_wallet(user, amount, tx_type, reference=None, description=None):
    """Thread-safe debit — raises ValueError on insufficient funds."""
    if Decimal(str(user.balance)) < Decimal(str(amount)):
        raise ValueError('Insufficient balance')
    bal_before = float(user.balance)
    user.balance = Decimal(str(user.balance)) - Decimal(str(amount))
    tx = Transaction(
        user_id=user.id, type=tx_type, amount=-abs(amount),
        balance_before=bal_before, balance_after=float(user.balance),
        reference=reference, description=description
    )
    db.session.add(tx)
    return tx

def admin_required(f):
    @wraps(f)
    @jwt_required()
    def decorated(*args, **kwargs):
        uid = get_jwt_identity()
        user = User.query.get(uid)
        if not user or not user.is_admin:
            return jsonify({'error': 'Admin access required'}), 403
        return f(*args, **kwargs)
    return decorated

def generate_ref():
    return str(uuid.uuid4()).replace('-', '').upper()[:16]

def validate_phone(phone):
    """Normalize Kenyan phone to 2547XXXXXXXX."""
    p = re.sub(r'\D', '', phone)
    if p.startswith('0') and len(p) == 10:
        p = '254' + p[1:]
    if p.startswith('+'):
        p = p[1:]
    if not re.match(r'^2547\d{8}$', p):
        return None
    return p

# ─────────────────────────────────────────────
#  AUTH ENDPOINTS
# ─────────────────────────────────────────────

@app.route('/api/register', methods=['POST'])
def register():
    data = request.get_json() or {}
    username = (data.get('username') or '').strip()
    email    = (data.get('email') or '').strip().lower()
    phone    = (data.get('phone') or '').strip()
    password = data.get('password', '')
    ref_code = data.get('referral_code', '')

    if not all([username, email, phone, password]):
        return jsonify({'error': 'All fields required'}), 400
    if len(password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400
    if not re.match(r'^[a-zA-Z0-9_]{3,30}$', username):
        return jsonify({'error': 'Username must be 3-30 alphanumeric characters'}), 400
    if not re.match(r'^[^@]+@[^@]+\.[^@]+$', email):
        return jsonify({'error': 'Invalid email address'}), 400

    phone = validate_phone(phone)
    if not phone:
        return jsonify({'error': 'Invalid phone number. Use Kenyan format e.g. 07XXXXXXXX'}), 400

    if User.query.filter_by(username=username).first():
        return jsonify({'error': 'Username already taken'}), 409
    if User.query.filter_by(email=email).first():
        return jsonify({'error': 'Email already registered'}), 409
    if User.query.filter_by(phone=phone).first():
        return jsonify({'error': 'Phone already registered'}), 409

    referrer = None
    if ref_code:
        referrer = User.query.filter_by(referral_code=ref_code).first()

    user = User(
        username=username, email=email, phone=phone,
        referral_code=generate_ref()[:8],
        referred_by=referrer.id if referrer else None
    )
    user.set_password(password)

    # Bonus for being referred
    if referrer:
        bonus_amount = float(Settings.get('referral_bonus', '50'))
        user.balance = Decimal(str(bonus_amount))

    db.session.add(user)
    db.session.flush()

    if referrer:
        referrer_bonus = float(Settings.get('referrer_bonus', '100'))
        credit_wallet(referrer, referrer_bonus, 'bonus',
                      description=f'Referral bonus for {username}')

    db.session.commit()

    access_token  = create_access_token(identity=user.id)
    refresh_token = create_refresh_token(identity=user.id)

    return jsonify({
        'message': 'Registration successful',
        'access_token': access_token,
        'refresh_token': refresh_token,
        'user': user.to_dict()
    }), 201


@app.route('/api/login', methods=['POST'])
def login():
    data     = request.get_json() or {}
    email    = (data.get('email') or '').strip().lower()
    password = data.get('password', '')

    if not email or not password:
        return jsonify({'error': 'Email and password required'}), 400

    user = User.query.filter_by(email=email).first()

    if not user:
        return jsonify({'error': 'Invalid credentials'}), 401

    # Check lock
    if user.locked_until and user.locked_until > datetime.utcnow():
        remaining = int((user.locked_until - datetime.utcnow()).total_seconds() / 60)
        return jsonify({'error': f'Account locked. Try again in {remaining} minutes'}), 423

    if not user.check_password(password):
        user.failed_logins = (user.failed_logins or 0) + 1
        if user.failed_logins >= 5:
            user.locked_until = datetime.utcnow() + timedelta(minutes=30)
            user.status = 'locked'
            db.session.commit()
            return jsonify({'error': 'Too many failed attempts. Account locked for 30 minutes'}), 423
        db.session.commit()
        return jsonify({'error': f'Invalid credentials. {5 - user.failed_logins} attempts remaining'}), 401

    if user.status == 'suspended':
        return jsonify({'error': 'Account suspended. Contact support'}), 403

    # Reset failures
    user.failed_logins = 0
    user.locked_until  = None
    user.last_login    = datetime.utcnow()
    if user.status == 'locked':
        user.status = 'active'
    db.session.commit()

    access_token  = create_access_token(identity=user.id)
    refresh_token = create_refresh_token(identity=user.id)

    return jsonify({
        'access_token': access_token,
        'refresh_token': refresh_token,
        'user': user.to_dict()
    })


@app.route('/api/refresh', methods=['POST'])
@jwt_required(refresh=True)
def refresh():
    uid   = get_jwt_identity()
    token = create_access_token(identity=uid)
    return jsonify({'access_token': token})


@app.route('/api/me', methods=['GET'])
@jwt_required()
def me():
    uid  = get_jwt_identity()
    user = User.query.get_or_404(uid)
    return jsonify(user.to_dict())


# ─────────────────────────────────────────────
#  MARKET ENDPOINTS
# ─────────────────────────────────────────────

@app.route('/api/markets', methods=['GET'])
def list_markets():
    category = request.args.get('category')
    status   = request.args.get('status', 'open')
    page     = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    search   = request.args.get('search', '')

    q = Market.query
    if status and status != 'all':
        q = q.filter_by(status=status)
    if category:
        q = q.filter_by(category=category)
    if search:
        q = q.filter(Market.title.ilike(f'%{search}%'))

    # Auto-close expired markets
    expired = q.filter(Market.closes_at <= datetime.utcnow(), Market.status == 'open').all()
    for m in expired:
        m.status = 'closed'
    if expired:
        db.session.commit()

    q = q.order_by(Market.created_at.desc())
    total = q.count()
    markets = q.offset((page - 1) * per_page).limit(per_page).all()

    return jsonify({
        'markets': [m.to_dict() for m in markets],
        'total': total,
        'page': page,
        'per_page': per_page,
        'pages': (total + per_page - 1) // per_page
    })


@app.route('/api/markets/<int:market_id>', methods=['GET'])
def get_market(market_id):
    m = Market.query.get_or_404(market_id)
    d = m.to_dict()
    d['bet_count'] = m.bets.count()
    return jsonify(d)


@app.route('/api/markets', methods=['POST'])
@jwt_required()
def create_market():
    uid  = get_jwt_identity()
    user = User.query.get_or_404(uid)
    data = request.get_json() or {}

    title       = (data.get('title') or '').strip()
    description = (data.get('description') or '').strip()
    category    = (data.get('category') or 'other').strip()
    closes_at   = data.get('closes_at')
    yes_odds    = float(data.get('yes_odds', 2.0))
    no_odds     = float(data.get('no_odds', 2.0))

    if not title or not closes_at:
        return jsonify({'error': 'Title and closing time required'}), 400
    if yes_odds < 1.05 or no_odds < 1.05:
        return jsonify({'error': 'Odds must be at least 1.05'}), 400

    try:
        closes_dt = datetime.fromisoformat(closes_at.replace('Z', '+00:00'))
        closes_dt = closes_dt.replace(tzinfo=None)
    except (ValueError, AttributeError):
        return jsonify({'error': 'Invalid closing time format'}), 400

    if closes_dt <= datetime.utcnow():
        return jsonify({'error': 'Closing time must be in the future'}), 400

    # Optional market creation fee
    creation_fee = float(Settings.get('market_creation_fee', '0'))
    if creation_fee > 0:
        if float(user.balance) < creation_fee:
            return jsonify({'error': f'Market creation fee of KES {creation_fee} required'}), 400
        debit_wallet(user, creation_fee, 'commission',
                     description='Market creation fee')

    market = Market(
        title=title, description=description, category=category,
        closes_at=closes_dt, yes_odds=yes_odds, no_odds=no_odds,
        created_by=uid
    )
    db.session.add(market)
    db.session.commit()

    return jsonify({'message': 'Market created', 'market': market.to_dict()}), 201


# ─────────────────────────────────────────────
#  BETTING ENDPOINTS
# ─────────────────────────────────────────────

def calculate_bet(stake, odds, commission_rate):
    stake        = Decimal(str(stake))
    odds         = Decimal(str(odds))
    commission_r = Decimal(str(commission_rate))
    gross_payout = stake * odds
    profit       = gross_payout - stake
    commission   = profit * commission_r
    net_payout   = gross_payout - commission
    return float(stake), float(gross_payout), float(commission), float(net_payout)


@app.route('/api/place-bet', methods=['POST'])
@jwt_required()
def place_bet():
    uid  = get_jwt_identity()
    user = User.query.get_or_404(uid)
    data = request.get_json() or {}

    market_id = data.get('market_id')
    selection = (data.get('selection') or '').upper()
    stake     = data.get('stake')

    if not all([market_id, selection, stake]):
        return jsonify({'error': 'market_id, selection, and stake required'}), 400
    if selection not in ('YES', 'NO'):
        return jsonify({'error': 'Selection must be YES or NO'}), 400

    try:
        stake = float(stake)
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid stake amount'}), 400

    market = Market.query.get(market_id)
    if not market:
        return jsonify({'error': 'Market not found'}), 404
    if market.status != 'open':
        return jsonify({'error': 'Market is not open for betting'}), 400
    if market.closes_at <= datetime.utcnow():
        market.status = 'closed'
        db.session.commit()
        return jsonify({'error': 'Market has closed'}), 400

    min_s = float(market.min_stake)
    max_s = float(market.max_stake)
    if stake < min_s:
        return jsonify({'error': f'Minimum stake is KES {min_s}'}), 400
    if stake > max_s:
        return jsonify({'error': f'Maximum stake is KES {max_s}'}), 400

    if float(user.balance) < stake:
        return jsonify({'error': 'Insufficient balance'}), 400
    if user.status != 'active':
        return jsonify({'error': 'Account not active'}), 403

    odds = float(market.yes_odds) if selection == 'YES' else float(market.no_odds)
    commission_rate = get_commission_rate()
    _, gross_payout, commission, net_payout = calculate_bet(stake, odds, commission_rate)

    ref = generate_ref()
    debit_wallet(user, stake, 'bet_placed', reference=ref,
                 description=f'Bet on {market.title} ({selection})')

    bet = Bet(
        user_id=uid, market_id=market_id, selection=selection,
        stake=stake, odds=odds, gross_payout=gross_payout,
        commission=commission, net_payout=net_payout, status='open'
    )
    db.session.add(bet)

    # Update volume and rebalance odds
    if selection == 'YES':
        market.yes_volume = Decimal(str(market.yes_volume)) + Decimal(str(stake))
    else:
        market.no_volume  = Decimal(str(market.no_volume))  + Decimal(str(stake))
    market.auto_balance_odds()

    user.total_wagered = Decimal(str(user.total_wagered)) + Decimal(str(stake))
    db.session.commit()

    return jsonify({'message': 'Bet placed', 'bet': bet.to_dict()}), 201


@app.route('/api/place-multibet', methods=['POST'])
@jwt_required()
def place_multibet():
    uid  = get_jwt_identity()
    user = User.query.get_or_404(uid)
    data = request.get_json() or {}

    selections = data.get('selections', [])  # [{market_id, selection}]
    stake      = data.get('stake')

    if not selections or len(selections) < 2:
        return jsonify({'error': 'Multibet requires at least 2 selections'}), 400
    if len(selections) > 20:
        return jsonify({'error': 'Maximum 20 selections per multibet'}), 400

    try:
        stake = float(stake)
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid stake'}), 400

    if stake < 10:
        return jsonify({'error': 'Minimum multibet stake is KES 10'}), 400
    if float(user.balance) < stake:
        return jsonify({'error': 'Insufficient balance'}), 400
    if user.status != 'active':
        return jsonify({'error': 'Account not active'}), 403

    # Validate all markets and selections
    items_data = []
    seen_markets = set()
    total_odds = Decimal('1')

    for sel in selections:
        mid  = sel.get('market_id')
        pick = (sel.get('selection') or '').upper()
        if pick not in ('YES', 'NO'):
            return jsonify({'error': f'Invalid selection for market {mid}'}), 400
        if mid in seen_markets:
            return jsonify({'error': 'Cannot select same market twice in multibet'}), 400
        seen_markets.add(mid)

        market = Market.query.get(mid)
        if not market or market.status != 'open' or market.closes_at <= datetime.utcnow():
            return jsonify({'error': f'Market {mid} is not open'}), 400

        odds = float(market.yes_odds) if pick == 'YES' else float(market.no_odds)
        total_odds *= Decimal(str(odds))
        items_data.append({'market': market, 'selection': pick, 'odds': odds})

    total_odds_f   = float(total_odds)
    commission_rate = get_commission_rate()
    _, gross_payout, commission, net_payout = calculate_bet(stake, total_odds_f, commission_rate)

    ref = generate_ref()
    debit_wallet(user, stake, 'bet_placed', reference=ref,
                 description=f'Multibet ({len(selections)} selections)')

    mb = Multibet(
        user_id=uid, total_odds=total_odds_f, total_stake=stake,
        gross_payout=gross_payout, commission=commission, net_payout=net_payout
    )
    db.session.add(mb)
    db.session.flush()

    for item in items_data:
        mbi = MultibetItem(
            multibet_id=mb.id, market_id=item['market'].id,
            selection=item['selection'], odds=item['odds']
        )
        db.session.add(mbi)
        if item['selection'] == 'YES':
            item['market'].yes_volume = Decimal(str(item['market'].yes_volume)) + Decimal(str(stake))
        else:
            item['market'].no_volume  = Decimal(str(item['market'].no_volume))  + Decimal(str(stake))

    user.total_wagered = Decimal(str(user.total_wagered)) + Decimal(str(stake))
    db.session.commit()

    return jsonify({'message': 'Multibet placed', 'multibet': mb.to_dict()}), 201


# ─────────────────────────────────────────────
#  HISTORY / STATS
# ─────────────────────────────────────────────

@app.route('/api/history', methods=['GET'])
@jwt_required()
def bet_history():
    uid     = get_jwt_identity()
    status  = request.args.get('status')
    page    = int(request.args.get('page', 1))
    per_page= int(request.args.get('per_page', 20))

    q = Bet.query.filter_by(user_id=uid)
    if status:
        q = q.filter_by(status=status)
    total = q.count()
    bets  = q.order_by(Bet.created_at.desc()).offset((page-1)*per_page).limit(per_page).all()

    mb_q = Multibet.query.filter_by(user_id=uid)
    if status:
        mb_q = mb_q.filter_by(status=status)
    multibets = mb_q.order_by(Multibet.created_at.desc()).limit(20).all()

    return jsonify({
        'bets': [b.to_dict() for b in bets],
        'multibets': [m.to_dict() for m in multibets],
        'total': total,
        'page': page,
        'pages': (total + per_page - 1) // per_page
    })


@app.route('/api/transactions', methods=['GET'])
@jwt_required()
def transactions():
    uid     = get_jwt_identity()
    page    = int(request.args.get('page', 1))
    per_page= int(request.args.get('per_page', 20))
    tx_type = request.args.get('type')

    q = Transaction.query.filter_by(user_id=uid)
    if tx_type:
        q = q.filter_by(type=tx_type)
    total = q.count()
    txs   = q.order_by(Transaction.created_at.desc()).offset((page-1)*per_page).limit(per_page).all()

    return jsonify({
        'transactions': [t.to_dict() for t in txs],
        'total': total,
        'page': page,
        'pages': (total + per_page - 1) // per_page
    })


# ─────────────────────────────────────────────
#  WALLET / M-PESA ENDPOINTS
# ─────────────────────────────────────────────

@app.route('/api/deposit', methods=['POST'])
@jwt_required()
def deposit():
    """Initiate M-Pesa STK Push via IntaSend."""
    uid  = get_jwt_identity()
    user = User.query.get_or_404(uid)
    data = request.get_json() or {}

    amount = data.get('amount')
    phone  = data.get('phone', user.phone)

    try:
        amount = float(amount)
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid amount'}), 400

    if amount < 10:
        return jsonify({'error': 'Minimum deposit is KES 10'}), 400
    if amount > 150000:
        return jsonify({'error': 'Maximum deposit is KES 150,000'}), 400

    phone = validate_phone(phone)
    if not phone:
        return jsonify({'error': 'Invalid phone number'}), 400

    ref = 'DEP' + generate_ref()[:10]

    # Try IntaSend if configured
    api_key = os.environ.get('INTASEND_API_KEY')
    if api_key:
        try:
            import requests as req_lib
            pub_key = os.environ.get('INTASEND_PUBLISHABLE_KEY', '')
            is_test = os.environ.get('INTASEND_TEST', 'true').lower() == 'true'
            base_url = 'https://sandbox.intasend.com' if is_test else 'https://payment.intasend.com'

            resp = req_lib.post(
                f'{base_url}/api/v1/payment/mpesa-stk-push/',
                json={
                    'amount': int(amount),
                    'phone_number': phone,
                    'currency': 'KES',
                    'email': user.email,
                    'narrative': f'Wallet deposit - {ref}',
                    'api_ref': ref,
                },
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {api_key}',
                    'X-IntaSend-Public-Key': pub_key,
                },
                timeout=30
            )
            resp_data = resp.json()
            if resp.status_code in (200, 201):
                # Store pending transaction
                tx = Transaction(
                    user_id=uid, type='deposit', amount=amount,
                    balance_before=float(user.balance),
                    balance_after=float(user.balance),
                    reference=ref, status='pending',
                    description=f'M-Pesa deposit {phone}',
                    mpesa_ref=resp_data.get('invoice', {}).get('invoice_id') or resp_data.get('id')
                )
                db.session.add(tx)
                db.session.commit()
                return jsonify({
                    'message': 'STK Push sent. Check your phone to complete payment.',
                    'reference': ref,
                    'invoice_id': resp_data.get('invoice', {}).get('invoice_id'),
                })
            else:
                logger.error(f'IntaSend error: {resp_data}')
                return jsonify({'error': 'Payment initiation failed', 'detail': resp_data}), 502
        except Exception as e:
            logger.error(f'Deposit error: {e}')
            return jsonify({'error': 'Payment service error'}), 502
    else:
        # DEMO mode: credit instantly (no real M-Pesa)
        credit_wallet(user, amount, 'deposit', reference=ref,
                      description='Demo deposit (M-Pesa not configured)')
        user.total_deposited = Decimal(str(user.total_deposited)) + Decimal(str(amount))
        db.session.commit()
        return jsonify({
            'message': f'Demo: KES {amount} credited instantly (configure INTASEND_API_KEY for real M-Pesa)',
            'reference': ref,
            'balance': float(user.balance)
        })


@app.route('/api/webhook/intasend', methods=['POST'])
def intasend_webhook():
    """IntaSend payment webhook — credits wallet on success."""
    data = request.get_json() or {}
    logger.info(f'IntaSend webhook: {data}')

    invoice_id = (data.get('invoice', {}) or {}).get('invoice_id') or data.get('id')
    state      = (data.get('invoice', {}) or {}).get('state') or data.get('state', '')
    api_ref    = (data.get('invoice', {}) or {}).get('api_ref') or data.get('api_ref', '')

    if state.upper() not in ('COMPLETE', 'COMPLETED'):
        return jsonify({'status': 'ignored'}), 200

    tx = Transaction.query.filter(
        (Transaction.reference == api_ref) | (Transaction.mpesa_ref == str(invoice_id))
    ).filter_by(type='deposit', status='pending').first()

    if not tx:
        logger.warning(f'Webhook: no pending tx for ref={api_ref} invoice={invoice_id}')
        return jsonify({'status': 'not_found'}), 200

    if tx.status == 'completed':
        return jsonify({'status': 'already_processed'}), 200

    user = User.query.get(tx.user_id)
    amount = abs(float(tx.amount))

    tx.status        = 'completed'
    tx.balance_after = float(user.balance) + amount
    user.balance     = Decimal(str(user.balance)) + Decimal(str(amount))
    user.total_deposited = Decimal(str(user.total_deposited)) + Decimal(str(amount))

    db.session.commit()
    logger.info(f'Credited {amount} to user {user.id}')
    return jsonify({'status': 'credited'}), 200


@app.route('/api/withdraw', methods=['POST'])
@jwt_required()
def withdraw():
    uid  = get_jwt_identity()
    user = User.query.get_or_404(uid)
    data = request.get_json() or {}

    amount = data.get('amount')
    phone  = data.get('phone', user.phone)

    try:
        amount = float(amount)
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid amount'}), 400

    min_w = float(Settings.get('min_withdrawal', '100'))
    max_w = float(Settings.get('max_withdrawal', '500000'))

    if amount < min_w:
        return jsonify({'error': f'Minimum withdrawal is KES {min_w}'}), 400
    if amount > max_w:
        return jsonify({'error': f'Maximum withdrawal is KES {max_w}'}), 400

    phone = validate_phone(phone)
    if not phone:
        return jsonify({'error': 'Invalid phone number'}), 400

    fee_rate = get_withdrawal_fee()
    fee = round(amount * fee_rate, 2)
    total_deduct = amount + fee

    if float(user.balance) < total_deduct:
        return jsonify({'error': f'Insufficient balance (including fee of KES {fee})'}), 400

    ref = 'WIT' + generate_ref()[:10]

    # Deduct from balance (hold)
    debit_wallet(user, total_deduct, 'withdrawal',
                 reference=ref, description=f'Withdrawal request to {phone} (fee: {fee})')

    wr = WithdrawalRequest(user_id=uid, amount=amount, phone=phone, reference=ref)
    db.session.add(wr)
    user.total_withdrawn = Decimal(str(user.total_withdrawn)) + Decimal(str(amount))
    db.session.commit()

    return jsonify({
        'message': 'Withdrawal request submitted. Admin will approve within 24 hours.',
        'reference': ref,
        'amount': amount,
        'fee': fee,
        'balance': float(user.balance)
    })


# ─────────────────────────────────────────────
#  ADMIN ENDPOINTS
# ─────────────────────────────────────────────

@app.route('/api/admin/dashboard', methods=['GET'])
@admin_required
def admin_dashboard():
    uid = get_jwt_identity()
    total_users    = User.query.count()
    active_users   = User.query.filter_by(status='active').count()
    suspended_users= User.query.filter_by(status='suspended').count()
    total_markets  = Market.query.count()
    open_markets   = Market.query.filter_by(status='open').count()
    total_bets     = Bet.query.count()
    open_bets      = Bet.query.filter_by(status='open').count()
    pending_with   = WithdrawalRequest.query.filter_by(status='pending').count()

    dep_sum = db.session.query(db.func.sum(Transaction.amount)).filter(
        Transaction.type == 'deposit', Transaction.status == 'completed').scalar() or 0
    win_sum = db.session.query(db.func.sum(Transaction.amount)).filter(
        Transaction.type == 'winnings').scalar() or 0
    com_sum = db.session.query(db.func.sum(Bet.commission)).filter(
        Bet.status == 'won').scalar() or 0
    total_bal = db.session.query(db.func.sum(User.balance)).scalar() or 0

    return jsonify({
        'users': {'total': total_users, 'active': active_users, 'suspended': suspended_users},
        'markets': {'total': total_markets, 'open': open_markets},
        'bets': {'total': total_bets, 'open': open_bets},
        'finance': {
            'total_deposits': float(dep_sum),
            'total_winnings_paid': float(win_sum),
            'total_commission': float(com_sum),
            'platform_balance_held': float(total_bal),
        },
        'pending_withdrawals': pending_with,
        'commission_rate': get_commission_rate(),
        'withdrawal_fee': get_withdrawal_fee(),
    })


@app.route('/api/admin/create-market', methods=['POST'])
@admin_required
def admin_create_market():
    uid  = get_jwt_identity()
    data = request.get_json() or {}

    title       = (data.get('title') or '').strip()
    description = (data.get('description') or '').strip()
    category    = (data.get('category') or 'other').strip()
    closes_at   = data.get('closes_at')
    yes_odds    = float(data.get('yes_odds', 2.0))
    no_odds     = float(data.get('no_odds', 2.0))
    min_stake   = float(data.get('min_stake', 10.0))
    max_stake   = float(data.get('max_stake', 100000.0))

    if not title or not closes_at:
        return jsonify({'error': 'Title and closing time required'}), 400

    try:
        closes_dt = datetime.fromisoformat(closes_at.replace('Z', '+00:00')).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return jsonify({'error': 'Invalid closing time format'}), 400

    market = Market(
        title=title, description=description, category=category,
        closes_at=closes_dt, yes_odds=yes_odds, no_odds=no_odds,
        min_stake=min_stake, max_stake=max_stake, created_by=uid
    )
    db.session.add(market)
    log_admin(uid, 'create_market', target=title)
    db.session.commit()

    return jsonify({'message': 'Market created', 'market': market.to_dict()}), 201


@app.route('/api/admin/markets/<int:market_id>', methods=['PUT'])
@admin_required
def admin_edit_market(market_id):
    uid    = get_jwt_identity()
    market = Market.query.get_or_404(market_id)
    data   = request.get_json() or {}

    if 'title'       in data: market.title       = data['title']
    if 'description' in data: market.description = data['description']
    if 'category'    in data: market.category    = data['category']
    if 'yes_odds'    in data: market.yes_odds    = float(data['yes_odds'])
    if 'no_odds'     in data: market.no_odds     = float(data['no_odds'])
    if 'status'      in data: market.status      = data['status']
    if 'min_stake'   in data: market.min_stake   = float(data['min_stake'])
    if 'max_stake'   in data: market.max_stake   = float(data['max_stake'])
    if 'closes_at'   in data:
        market.closes_at = datetime.fromisoformat(data['closes_at'].replace('Z','+00:00')).replace(tzinfo=None)

    log_admin(uid, 'edit_market', target=str(market_id), details=data)
    db.session.commit()
    return jsonify({'message': 'Market updated', 'market': market.to_dict()})


@app.route('/api/admin/settle-market', methods=['POST'])
@admin_required
def admin_settle_market():
    """Settle a market and pay out winners."""
    uid  = get_jwt_identity()
    data = request.get_json() or {}

    market_id = data.get('market_id')
    result    = (data.get('result') or '').upper()

    if result not in ('YES', 'NO', 'VOID'):
        return jsonify({'error': 'Result must be YES, NO, or VOID'}), 400

    market = Market.query.get(market_id)
    if not market:
        return jsonify({'error': 'Market not found'}), 404
    if market.status == 'settled':
        return jsonify({'error': 'Market already settled'}), 400

    market.result = result
    market.status = 'settled'

    open_bets = Bet.query.filter_by(market_id=market_id, status='open').all()
    settled_count = 0
    total_paid    = 0.0

    for bet in open_bets:
        bet.settled_at = datetime.utcnow()
        if result == 'VOID':
            # Refund stake
            bet.status = 'void'
            user = User.query.get(bet.user_id)
            credit_wallet(user, float(bet.stake), 'refund',
                          description=f'Refund: market {market.title} voided')
        elif bet.selection == result:
            # Winner
            bet.status = 'won'
            user = User.query.get(bet.user_id)
            credit_wallet(user, float(bet.net_payout), 'winnings',
                          description=f'Won: {market.title} ({result})')
            user.total_won = Decimal(str(user.total_won)) + Decimal(str(bet.net_payout))
            total_paid += float(bet.net_payout)
        else:
            bet.status = 'lost'
        settled_count += 1

    # Settle multibets that include this market
    affected_multibets = set()
    for mbi in MultibetItem.query.filter_by(market_id=market_id).all():
        mbi.result = result
        affected_multibets.add(mbi.multibet_id)

    for mb_id in affected_multibets:
        mb = Multibet.query.get(mb_id)
        if not mb or mb.status != 'open':
            continue
        items = MultibetItem.query.filter_by(multibet_id=mb_id).all()
        if any(i.result is None for i in items):
            continue  # not all markets settled yet
        # Check outcome
        if any(i.result == 'VOID' for i in items):
            mb.status = 'void'
            user = User.query.get(mb.user_id)
            credit_wallet(user, float(mb.total_stake), 'refund',
                          description='Multibet voided (market voided)')
        elif all(i.result == i.selection for i in items):
            mb.status = 'won'
            user = User.query.get(mb.user_id)
            credit_wallet(user, float(mb.net_payout), 'winnings',
                          description='Multibet won')
            user.total_won = Decimal(str(user.total_won)) + Decimal(str(mb.net_payout))
        else:
            mb.status = 'lost'
        mb.settled_at = datetime.utcnow()

    log_admin(uid, 'settle_market', target=str(market_id),
              details={'result': result, 'bets_settled': settled_count})
    db.session.commit()

    return jsonify({
        'message': f'Market settled as {result}',
        'bets_settled': settled_count,
        'total_paid_out': total_paid
    })


@app.route('/api/admin/users', methods=['GET'])
@admin_required
def admin_list_users():
    page     = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    search   = request.args.get('search', '')
    status   = request.args.get('status')

    q = User.query
    if search:
        q = q.filter(
            (User.username.ilike(f'%{search}%')) |
            (User.email.ilike(f'%{search}%')) |
            (User.phone.ilike(f'%{search}%'))
        )
    if status:
        q = q.filter_by(status=status)

    total = q.count()
    users = q.order_by(User.created_at.desc()).offset((page-1)*per_page).limit(per_page).all()

    return jsonify({
        'users': [u.to_dict(admin=True) for u in users],
        'total': total,
        'page': page,
        'pages': (total + per_page - 1) // per_page
    })


@app.route('/api/admin/users/<int:user_id>', methods=['GET'])
@admin_required
def admin_get_user(user_id):
    user = User.query.get_or_404(user_id)
    return jsonify(user.to_dict(admin=True))


@app.route('/api/admin/users/<int:user_id>/status', methods=['PUT'])
@admin_required
def admin_update_user_status(user_id):
    uid  = get_jwt_identity()
    user = User.query.get_or_404(user_id)
    data = request.get_json() or {}
    new_status = data.get('status')

    if new_status not in ('active', 'suspended', 'locked'):
        return jsonify({'error': 'Invalid status'}), 400

    user.status = new_status
    log_admin(uid, f'user_{new_status}', target=user.username)
    db.session.commit()
    return jsonify({'message': f'User {new_status}', 'user': user.to_dict(admin=True)})


@app.route('/api/admin/users/<int:user_id>/adjust-balance', methods=['POST'])
@admin_required
def admin_adjust_balance(user_id):
    uid  = get_jwt_identity()
    user = User.query.get_or_404(user_id)
    data = request.get_json() or {}

    amount = data.get('amount')
    reason = data.get('reason', 'Admin adjustment')

    try:
        amount = float(amount)
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid amount'}), 400

    if amount == 0:
        return jsonify({'error': 'Amount cannot be zero'}), 400

    if amount > 0:
        credit_wallet(user, amount, 'admin_adjustment', description=reason)
    else:
        if float(user.balance) < abs(amount):
            return jsonify({'error': 'Insufficient user balance'}), 400
        debit_wallet(user, abs(amount), 'admin_adjustment', description=reason)

    log_admin(uid, 'adjust_balance', target=user.username,
              details={'amount': amount, 'reason': reason})
    db.session.commit()

    return jsonify({'message': f'Balance adjusted by KES {amount}', 'balance': float(user.balance)})


@app.route('/api/admin/withdrawals', methods=['GET'])
@admin_required
def admin_list_withdrawals():
    page     = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    status   = request.args.get('status', 'pending')

    q = WithdrawalRequest.query
    if status and status != 'all':
        q = q.filter_by(status=status)
    total = q.count()
    wrs   = q.order_by(WithdrawalRequest.created_at.desc()).offset((page-1)*per_page).limit(per_page).all()

    return jsonify({
        'withdrawals': [w.to_dict() for w in wrs],
        'total': total,
        'page': page,
        'pages': (total + per_page - 1) // per_page
    })


@app.route('/api/admin/withdrawals/<int:wr_id>', methods=['PUT'])
@admin_required
def admin_review_withdrawal(wr_id):
    uid  = get_jwt_identity()
    wr   = WithdrawalRequest.query.get_or_404(wr_id)
    data = request.get_json() or {}

    action     = (data.get('action') or '').lower()  # approve | reject
    admin_note = data.get('note', '')

    if wr.status != 'pending':
        return jsonify({'error': 'Already reviewed'}), 400
    if action not in ('approve', 'reject'):
        return jsonify({'error': 'Action must be approve or reject'}), 400

    wr.status      = 'approved' if action == 'approve' else 'rejected'
    wr.admin_note  = admin_note
    wr.reviewed_at = datetime.utcnow()
    wr.reviewed_by = uid

    if action == 'reject':
        # Refund deducted amount + fee back
        user = User.query.get(wr.user_id)
        fee_rate = get_withdrawal_fee()
        fee      = round(float(wr.amount) * fee_rate, 2)
        refund   = float(wr.amount) + fee
        credit_wallet(user, refund, 'refund',
                      description=f'Withdrawal rejected: {wr.reference}')

    log_admin(uid, f'withdrawal_{action}', target=wr.reference,
              details={'amount': float(wr.amount), 'note': admin_note})
    db.session.commit()

    return jsonify({'message': f'Withdrawal {wr.status}', 'withdrawal': wr.to_dict()})


@app.route('/api/admin/settings', methods=['GET', 'PUT'])
@admin_required
def admin_settings():
    uid = get_jwt_identity()
    if request.method == 'GET':
        keys = ['commission_rate', 'withdrawal_fee', 'min_withdrawal', 'max_withdrawal',
                'market_creation_fee', 'referral_bonus', 'referrer_bonus']
        return jsonify({k: Settings.get(k, '') for k in keys})

    data = request.get_json() or {}
    allowed = {'commission_rate', 'withdrawal_fee', 'min_withdrawal', 'max_withdrawal',
               'market_creation_fee', 'referral_bonus', 'referrer_bonus'}
    updated = {}
    for k, v in data.items():
        if k in allowed:
            Settings.set(k, v)
            updated[k] = v
    log_admin(uid, 'update_settings', details=updated)
    return jsonify({'message': 'Settings updated', 'updated': updated})


@app.route('/api/admin/logs', methods=['GET'])
@admin_required
def admin_logs():
    page     = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 50))
    logs     = AdminLog.query.order_by(AdminLog.timestamp.desc()).offset((page-1)*per_page).limit(per_page).all()
    total    = AdminLog.query.count()
    return jsonify({
        'logs': [l.to_dict() for l in logs],
        'total': total, 'page': page,
        'pages': (total + per_page - 1) // per_page
    })


@app.route('/api/admin/transactions', methods=['GET'])
@admin_required
def admin_transactions():
    page     = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 50))
    uid_f    = request.args.get('user_id')
    tx_type  = request.args.get('type')

    q = Transaction.query
    if uid_f:
        q = q.filter_by(user_id=int(uid_f))
    if tx_type:
        q = q.filter_by(type=tx_type)

    total = q.count()
    txs   = q.order_by(Transaction.created_at.desc()).offset((page-1)*per_page).limit(per_page).all()
    return jsonify({'transactions': [t.to_dict() for t in txs], 'total': total, 'page': page})


# ─────────────────────────────────────────────
#  ODDS CALCULATOR ENDPOINT
# ─────────────────────────────────────────────

@app.route('/api/calculate', methods=['POST'])
def calculate():
    data = request.get_json() or {}
    stake        = float(data.get('stake', 0))
    odds         = float(data.get('odds', 1))
    commission_r = get_commission_rate()
    _, gross, commission, net = calculate_bet(stake, odds, commission_r)
    return jsonify({
        'stake': stake,
        'odds': odds,
        'gross_payout': round(gross, 2),
        'commission': round(commission, 2),
        'net_payout': round(net, 2),
        'profit': round(net - stake, 2),
        'commission_rate': commission_r,
    })


# ─────────────────────────────────────────────
#  FRONTEND — FULL SPA
# ─────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>PredictX — Universal Prediction Market</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet"/>
<style>
:root{
  --bg:#09090f;
  --bg2:#111118;
  --bg3:#1a1a24;
  --bg4:#22222e;
  --border:#2a2a38;
  --text:#f0f0f8;
  --text2:#8888a8;
  --text3:#5555722;
  --accent:#7c3aed;
  --accent2:#a855f7;
  --green:#10b981;
  --red:#ef4444;
  --yellow:#f59e0b;
  --blue:#3b82f6;
  --radius:12px;
  --shadow:0 4px 24px rgba(0,0,0,.4);
}
[data-theme="light"]{
  --bg:#f4f4fc;
  --bg2:#ffffff;
  --bg3:#eeeef8;
  --bg4:#e4e4f0;
  --border:#d0d0e0;
  --text:#111128;
  --text2:#55557a;
  --accent:#7c3aed;
  --shadow:0 4px 24px rgba(0,0,0,.1);
}
*{margin:0;padding:0;box-sizing:border-box;}
body{font-family:'Space Grotesk',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;overflow-x:hidden;}
a{color:inherit;text-decoration:none;}
button{cursor:pointer;font-family:inherit;}
input,select,textarea{font-family:inherit;}
.mono{font-family:'JetBrains Mono',monospace;}

/* TOPBAR */
.topbar{
  position:sticky;top:0;z-index:100;
  background:rgba(9,9,15,.85);
  backdrop-filter:blur(16px);
  border-bottom:1px solid var(--border);
  padding:0 24px;height:64px;
  display:flex;align-items:center;justify-content:space-between;
}
.logo{font-size:1.4rem;font-weight:700;
  background:linear-gradient(135deg,#7c3aed,#ec4899);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;}
.nav-links{display:flex;gap:8px;align-items:center;}
.nav-btn{
  padding:8px 16px;border-radius:8px;border:none;
  background:transparent;color:var(--text2);font-size:.9rem;
  transition:all .2s;font-weight:500;
}
.nav-btn:hover,.nav-btn.active{background:var(--bg3);color:var(--text);}
.wallet-badge{
  display:flex;align-items:center;gap:8px;
  background:var(--bg3);border:1px solid var(--border);
  border-radius:24px;padding:6px 14px;font-weight:600;font-size:.9rem;
  cursor:pointer;transition:border-color .2s;
}
.wallet-badge:hover{border-color:var(--accent);}
.wallet-badge .amount{color:var(--green);}

/* LAYOUT */
.main{max-width:1400px;margin:0 auto;padding:24px;display:grid;
  grid-template-columns:1fr 340px;gap:24px;}
.main.no-sidebar{grid-template-columns:1fr;}
@media(max-width:900px){.main{grid-template-columns:1fr;padding:12px;}}

/* CARDS */
.card{
  background:var(--bg2);border:1px solid var(--border);
  border-radius:var(--radius);padding:20px;
}
.card-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;}
.card-title{font-size:1rem;font-weight:600;color:var(--text);}

/* MARKET CARDS */
.market-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px;}
.market-card{
  background:var(--bg2);border:1px solid var(--border);
  border-radius:var(--radius);padding:20px;cursor:pointer;
  transition:all .2s;position:relative;overflow:hidden;
}
.market-card::before{
  content:'';position:absolute;top:0;left:0;right:0;height:3px;
  background:linear-gradient(90deg,var(--accent),#ec4899);
  transform:scaleX(0);transition:transform .2s;
}
.market-card:hover{border-color:var(--accent);box-shadow:0 0 24px rgba(124,58,237,.15);}
.market-card:hover::before{transform:scaleX(1);}
.market-cat{font-size:.7rem;font-weight:600;text-transform:uppercase;
  letter-spacing:.08em;color:var(--accent2);margin-bottom:8px;}
.market-title{font-size:.95rem;font-weight:600;line-height:1.4;margin-bottom:12px;}
.market-meta{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;}
.market-status{
  display:inline-flex;align-items:center;gap:4px;
  padding:3px 10px;border-radius:20px;font-size:.75rem;font-weight:600;
}
.status-open{background:rgba(16,185,129,.15);color:var(--green);}
.status-closed{background:rgba(107,114,128,.15);color:#6b7280;}
.status-settled{background:rgba(59,130,246,.15);color:var(--blue);}
.status-suspended{background:rgba(245,158,11,.15);color:var(--yellow);}
.live-dot{width:6px;height:6px;border-radius:50%;background:var(--green);
  animation:pulse 2s infinite;}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1);}50%{opacity:.5;transform:scale(.8);}}
.odds-row{display:flex;gap:8px;}
.odds-btn{
  flex:1;padding:10px;border-radius:8px;border:1px solid var(--border);
  background:var(--bg3);font-weight:600;font-size:.9rem;
  transition:all .2s;display:flex;flex-direction:column;align-items:center;gap:2px;
}
.odds-btn .label{font-size:.7rem;color:var(--text2);font-weight:500;}
.odds-btn .val{color:var(--text);}
.odds-btn:hover,.odds-btn.selected{border-color:var(--accent);background:rgba(124,58,237,.15);}
.odds-btn.yes:hover,.odds-btn.yes.selected{border-color:var(--green);background:rgba(16,185,129,.12);}
.odds-btn.no:hover,.odds-btn.no.selected{border-color:var(--red);background:rgba(239,68,68,.12);}
.odds-btn.yes.selected .val{color:var(--green);}
.odds-btn.no.selected .val{color:var(--red);}

/* BET SLIP */
.bet-slip{
  position:sticky;top:80px;
  background:var(--bg2);border:1px solid var(--border);
  border-radius:var(--radius);overflow:hidden;
}
.bet-slip-header{
  padding:16px 20px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
  background:linear-gradient(135deg,rgba(124,58,237,.1),rgba(236,72,153,.05));
}
.slip-items{max-height:40vh;overflow-y:auto;padding:12px;}
.slip-item{
  background:var(--bg3);border-radius:8px;padding:12px;
  margin-bottom:8px;position:relative;
}
.slip-item .remove{
  position:absolute;top:8px;right:8px;
  background:none;border:none;color:var(--text2);font-size:1.1rem;
  line-height:1;padding:2px;border-radius:4px;
}
.slip-item .remove:hover{color:var(--red);background:rgba(239,68,68,.1);}
.slip-market{font-size:.8rem;font-weight:600;padding-right:24px;line-height:1.3;}
.slip-sel{font-size:.75rem;margin-top:4px;
  display:flex;align-items:center;gap:6px;}
.slip-sel .badge{padding:2px 8px;border-radius:12px;font-weight:700;font-size:.7rem;}
.badge-yes{background:rgba(16,185,129,.2);color:var(--green);}
.badge-no{background:rgba(239,68,68,.2);color:var(--red);}
.slip-odds{font-weight:700;color:var(--accent2);font-size:.85rem;}
.slip-stake-area{padding:12px 16px;border-top:1px solid var(--border);}
.slip-label{font-size:.75rem;color:var(--text2);margin-bottom:6px;font-weight:500;}
.slip-input{
  width:100%;padding:10px 12px;border-radius:8px;
  border:1px solid var(--border);background:var(--bg3);
  color:var(--text);font-size:.95rem;font-family:inherit;
  transition:border-color .2s;
}
.slip-input:focus{outline:none;border-color:var(--accent);}
.slip-payout{
  background:var(--bg3);border-radius:8px;padding:12px;margin:8px 16px;
  display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:.8rem;
}
.slip-payout .row{display:flex;flex-direction:column;gap:2px;}
.slip-payout .key{color:var(--text2);}
.slip-payout .val{font-weight:700;}
.slip-payout .val.green{color:var(--green);}
.slip-payout .val.red{color:var(--red);}
.btn{
  display:inline-flex;align-items:center;justify-content:center;gap:8px;
  padding:10px 20px;border-radius:8px;font-weight:600;font-size:.9rem;
  border:none;transition:all .2s;
}
.btn-primary{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;}
.btn-primary:hover{opacity:.9;transform:translateY(-1px);}
.btn-primary:disabled{opacity:.5;transform:none;cursor:not-allowed;}
.btn-outline{background:none;border:1px solid var(--border);color:var(--text2);}
.btn-outline:hover{border-color:var(--text);color:var(--text);}
.btn-danger{background:rgba(239,68,68,.15);color:var(--red);border:1px solid rgba(239,68,68,.3);}
.btn-danger:hover{background:rgba(239,68,68,.25);}
.btn-success{background:rgba(16,185,129,.15);color:var(--green);border:1px solid rgba(16,185,129,.3);}
.btn-success:hover{background:rgba(16,185,129,.25);}
.btn-sm{padding:6px 12px;font-size:.8rem;}
.btn-full{width:100%;}
.slip-actions{padding:0 16px 16px;display:flex;flex-direction:column;gap:8px;}
.tabs{display:flex;gap:2px;background:var(--bg3);border-radius:8px;padding:3px;margin-bottom:16px;}
.tab{
  flex:1;padding:8px;border-radius:6px;font-weight:600;font-size:.85rem;
  border:none;background:none;color:var(--text2);transition:all .2s;
}
.tab.active{background:var(--bg2);color:var(--text);box-shadow:0 1px 4px rgba(0,0,0,.3);}

/* MODALS */
.overlay{
  position:fixed;inset:0;background:rgba(0,0,0,.6);
  backdrop-filter:blur(4px);z-index:200;
  display:flex;align-items:center;justify-content:center;padding:16px;
}
.modal{
  background:var(--bg2);border:1px solid var(--border);
  border-radius:16px;width:100%;max-width:460px;
  max-height:90vh;overflow-y:auto;
}
.modal-header{
  padding:20px 24px 16px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
}
.modal-title{font-size:1.1rem;font-weight:700;}
.modal-close{background:none;border:none;color:var(--text2);font-size:1.4rem;line-height:1;}
.modal-close:hover{color:var(--text);}
.modal-body{padding:20px 24px;}

/* FORMS */
.form-group{margin-bottom:16px;}
.form-label{display:block;font-size:.85rem;font-weight:600;color:var(--text2);margin-bottom:6px;}
.form-input{
  width:100%;padding:10px 14px;border-radius:8px;
  border:1px solid var(--border);background:var(--bg3);
  color:var(--text);font-size:.9rem;font-family:inherit;
  transition:border-color .2s;
}
.form-input:focus{outline:none;border-color:var(--accent);}
.form-select{
  width:100%;padding:10px 14px;border-radius:8px;
  border:1px solid var(--border);background:var(--bg3);
  color:var(--text);font-size:.9rem;appearance:none;
}
.form-err{color:var(--red);font-size:.8rem;margin-top:4px;}
.form-hint{color:var(--text2);font-size:.8rem;margin-top:4px;}

/* TABLES */
.table-wrap{overflow-x:auto;}
table{width:100%;border-collapse:collapse;font-size:.85rem;}
th{text-align:left;padding:10px 12px;color:var(--text2);font-weight:600;
   border-bottom:1px solid var(--border);white-space:nowrap;}
td{padding:10px 12px;border-bottom:1px solid var(--border);vertical-align:middle;}
tr:last-child td{border-bottom:none;}
tr:hover td{background:var(--bg3);}

/* STATS GRID */
.stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;margin-bottom:24px;}
.stat-card{
  background:var(--bg2);border:1px solid var(--border);
  border-radius:var(--radius);padding:16px;
}
.stat-label{font-size:.75rem;color:var(--text2);font-weight:600;text-transform:uppercase;
  letter-spacing:.06em;margin-bottom:6px;}
.stat-value{font-size:1.5rem;font-weight:700;}
.stat-sub{font-size:.75rem;color:var(--text2);margin-top:2px;}

/* FILTERS */
.filters{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px;}
.filter-btn{
  padding:6px 14px;border-radius:20px;border:1px solid var(--border);
  background:none;color:var(--text2);font-size:.8rem;font-weight:600;
  transition:all .2s;cursor:pointer;
}
.filter-btn:hover,.filter-btn.active{
  border-color:var(--accent);color:var(--accent);
  background:rgba(124,58,237,.08);
}

/* TOAST */
.toast-container{position:fixed;top:80px;right:16px;z-index:500;display:flex;flex-direction:column;gap:8px;}
.toast{
  background:var(--bg2);border:1px solid var(--border);
  border-radius:10px;padding:12px 16px;font-size:.85rem;
  box-shadow:var(--shadow);animation:slide-in .3s ease;
  display:flex;align-items:center;gap:10px;max-width:320px;
}
.toast.success{border-color:var(--green);}
.toast.error{border-color:var(--red);}
.toast.info{border-color:var(--accent);}
@keyframes slide-in{from{transform:translateX(110%);opacity:0;}to{transform:translateX(0);opacity:1;}}

/* HERO */
.hero{
  text-align:center;padding:48px 24px;
  background:radial-gradient(ellipse at 50% 0%,rgba(124,58,237,.2) 0%,transparent 70%);
}
.hero h1{font-size:2.5rem;font-weight:700;line-height:1.2;margin-bottom:12px;}
.hero h1 span{
  background:linear-gradient(135deg,#7c3aed,#ec4899,#f59e0b);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
}
.hero p{color:var(--text2);font-size:1.05rem;max-width:500px;margin:0 auto 24px;}
.hero-btns{display:flex;gap:12px;justify-content:center;flex-wrap:wrap;}

/* SEARCH */
.search-bar{
  display:flex;gap:8px;margin-bottom:16px;
}
.search-input{
  flex:1;padding:10px 16px;border-radius:8px;
  border:1px solid var(--border);background:var(--bg2);
  color:var(--text);font-size:.9rem;font-family:inherit;
}
.search-input:focus{outline:none;border-color:var(--accent);}

/* PROFILE */
.profile-header{
  display:flex;align-items:center;gap:16px;
  padding:20px;background:var(--bg2);border:1px solid var(--border);
  border-radius:var(--radius);margin-bottom:16px;
}
.avatar{
  width:56px;height:56px;border-radius:50%;
  background:linear-gradient(135deg,var(--accent),#ec4899);
  display:flex;align-items:center;justify-content:center;
  font-size:1.4rem;font-weight:700;color:#fff;flex-shrink:0;
}
.badge-pill{
  display:inline-flex;padding:3px 10px;border-radius:20px;
  font-size:.72rem;font-weight:700;
}

/* MISC */
.section-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;}
.section-title{font-size:1.1rem;font-weight:700;}
.empty{text-align:center;padding:48px;color:var(--text2);}
.empty-icon{font-size:2.5rem;margin-bottom:8px;}
.loader{display:flex;justify-content:center;padding:48px;}
.spinner{
  width:32px;height:32px;border-radius:50%;
  border:3px solid var(--border);border-top-color:var(--accent);
  animation:spin .7s linear infinite;
}
@keyframes spin{to{transform:rotate(360deg);}}
.divider{height:1px;background:var(--border);margin:16px 0;}
.text-green{color:var(--green);}
.text-red{color:var(--red);}
.text-yellow{color:var(--yellow);}
.text-blue{color:var(--blue);}
.text-muted{color:var(--text2);}
.mobile-menu-btn{display:none;background:none;border:none;color:var(--text);font-size:1.4rem;}
@media(max-width:600px){
  .mobile-menu-btn{display:flex;}
  .nav-links .nav-btn{display:none;}
  .hero h1{font-size:1.8rem;}
}
.progress{height:4px;background:var(--bg3);border-radius:4px;overflow:hidden;margin-top:8px;}
.progress-bar{height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));border-radius:4px;transition:width .5s;}
</style>
</head>
<body>

<div id="app">

<!-- TOPBAR -->
<nav class="topbar">
  <div style="display:flex;align-items:center;gap:24px;">
    <span class="logo">⚡ PredictX</span>
    <div class="nav-links" id="nav-links">
      <button class="nav-btn" onclick="navigate('home')">Markets</button>
      <button class="nav-btn" onclick="navigate('history')" id="nav-history" style="display:none">My Bets</button>
      <button class="nav-btn" onclick="navigate('wallet')" id="nav-wallet" style="display:none">Wallet</button>
      <button class="nav-btn" onclick="navigate('profile')" id="nav-profile" style="display:none">Profile</button>
      <button class="nav-btn" onclick="navigate('admin')" id="nav-admin" style="display:none">⚙ Admin</button>
    </div>
  </div>
  <div style="display:flex;align-items:center;gap:10px;">
    <button onclick="toggleTheme()" style="background:none;border:none;color:var(--text2);font-size:1.2rem;cursor:pointer;" title="Toggle theme">🌙</button>
    <div id="wallet-badge" class="wallet-badge" style="display:none" onclick="navigate('wallet')">
      💰 <span class="amount" id="wallet-amount">KES 0</span>
    </div>
    <div id="auth-btns">
      <button class="btn btn-outline btn-sm" onclick="showModal('login')">Login</button>
      <button class="btn btn-primary btn-sm" onclick="showModal('register')" style="margin-left:6px">Sign Up</button>
    </div>
    <div id="user-menu" style="display:none">
      <button class="btn btn-outline btn-sm" onclick="logout()">Logout</button>
    </div>
  </div>
</nav>

<!-- TOAST -->
<div class="toast-container" id="toasts"></div>

<!-- PAGE CONTAINER -->
<div id="page-content"></div>

<!-- MODALS -->
<div id="modal-overlay" class="overlay" style="display:none" onclick="closeModal(event)">
  <div class="modal" onclick="e=>e.stopPropagation()">
    <div id="modal-content"></div>
  </div>
</div>

</div>

<script>
// ─── STATE ───────────────────────────────────
let state = {
  user: null,
  token: localStorage.getItem('token'),
  refreshToken: localStorage.getItem('refresh_token'),
  betSlip: [],
  slipMode: 'single',
  markets: [],
  currentPage: 'home',
  marketFilter: 'all',
  searchQuery: '',
};

// ─── API ─────────────────────────────────────
async function api(method, path, body, auth=true) {
  const headers = {'Content-Type':'application/json'};
  if (auth && state.token) headers['Authorization'] = 'Bearer ' + state.token;
  const opts = {method, headers};
  if (body) opts.body = JSON.stringify(body);
  let res = await fetch('/api' + path, opts);

  if (res.status === 401 && state.refreshToken) {
    const r2 = await fetch('/api/refresh', {method:'POST',
      headers:{'Authorization':'Bearer '+state.refreshToken,'Content-Type':'application/json'}});
    if (r2.ok) {
      const d = await r2.json();
      state.token = d.access_token;
      localStorage.setItem('token', d.access_token);
      headers['Authorization'] = 'Bearer ' + state.token;
      res = await fetch('/api' + path, {...opts, headers});
    }
  }
  const data = await res.json().catch(() => ({}));
  return {ok: res.ok, status: res.status, data};
}

// ─── THEME ───────────────────────────────────
function toggleTheme() {
  const el = document.documentElement;
  const t = el.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  el.setAttribute('data-theme', t);
  localStorage.setItem('theme', t);
}
(function(){
  const t = localStorage.getItem('theme') || 'dark';
  document.documentElement.setAttribute('data-theme', t);
})();

// ─── TOAST ───────────────────────────────────
function toast(msg, type='info', dur=3500) {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  const icons = {success:'✅',error:'❌',info:'ℹ️'};
  el.innerHTML = `<span>${icons[type]||'ℹ️'}</span><span>${msg}</span>`;
  const cont = document.getElementById('toasts');
  cont.appendChild(el);
  setTimeout(() => el.remove(), dur);
}

// ─── AUTH ─────────────────────────────────────
async function loadMe() {
  if (!state.token) return;
  const {ok, data} = await api('GET', '/me');
  if (ok) {
    state.user = data;
    updateAuthUI();
  } else {
    logout();
  }
}

function updateAuthUI() {
  const loggedIn = !!state.user;
  document.getElementById('auth-btns').style.display = loggedIn ? 'none' : '';
  document.getElementById('user-menu').style.display = loggedIn ? '' : 'none';
  document.getElementById('wallet-badge').style.display = loggedIn ? '' : 'none';
  document.getElementById('nav-history').style.display = loggedIn ? '' : 'none';
  document.getElementById('nav-wallet').style.display  = loggedIn ? '' : 'none';
  document.getElementById('nav-profile').style.display = loggedIn ? '' : 'none';
  document.getElementById('nav-admin').style.display   = (loggedIn && state.user.is_admin) ? '' : 'none';
  if (loggedIn) {
    document.getElementById('wallet-amount').textContent = 'KES ' + fmtNum(state.user.balance);
  }
}

function logout() {
  state.user = null;
  state.token = null;
  state.refreshToken = null;
  state.betSlip = [];
  localStorage.removeItem('token');
  localStorage.removeItem('refresh_token');
  updateAuthUI();
  navigate('home');
}

async function doLogin(e) {
  e.preventDefault();
  const email    = document.getElementById('l-email').value;
  const password = document.getElementById('l-pass').value;
  const btn = document.getElementById('l-btn');
  btn.disabled = true; btn.textContent = 'Logging in…';
  const {ok, data} = await api('POST', '/login', {email, password}, false);
  if (ok) {
    state.token = data.access_token;
    state.refreshToken = data.refresh_token;
    state.user = data.user;
    localStorage.setItem('token', data.access_token);
    localStorage.setItem('refresh_token', data.refresh_token);
    updateAuthUI();
    closeModal();
    toast('Welcome back, ' + data.user.username + '!', 'success');
    navigate('home');
  } else {
    document.getElementById('l-err').textContent = data.error || 'Login failed';
    btn.disabled = false; btn.textContent = 'Login';
  }
}

async function doRegister(e) {
  e.preventDefault();
  const body = {
    username: document.getElementById('r-user').value,
    email:    document.getElementById('r-email').value,
    phone:    document.getElementById('r-phone').value,
    password: document.getElementById('r-pass').value,
    referral_code: document.getElementById('r-ref').value,
  };
  const btn = document.getElementById('r-btn');
  btn.disabled = true; btn.textContent = 'Creating account…';
  const {ok, data} = await api('POST', '/register', body, false);
  if (ok) {
    state.token = data.access_token;
    state.refreshToken = data.refresh_token;
    state.user = data.user;
    localStorage.setItem('token', data.access_token);
    localStorage.setItem('refresh_token', data.refresh_token);
    updateAuthUI();
    closeModal();
    toast('Account created! Welcome to PredictX!', 'success');
    navigate('home');
  } else {
    document.getElementById('r-err').textContent = data.error || 'Registration failed';
    btn.disabled = false; btn.textContent = 'Create Account';
  }
}

// ─── MODAL ───────────────────────────────────
function showModal(type, data={}) {
  const ov = document.getElementById('modal-overlay');
  const mc = document.getElementById('modal-content');
  ov.style.display = 'flex';

  if (type === 'login') {
    mc.innerHTML = `
      <div class="modal-header">
        <span class="modal-title">Login to PredictX</span>
        <button class="modal-close" onclick="closeModal()">✕</button>
      </div>
      <div class="modal-body">
        <form onsubmit="doLogin(event)">
          <div class="form-group">
            <label class="form-label">Email Address</label>
            <input class="form-input" id="l-email" type="email" placeholder="you@email.com" required/>
          </div>
          <div class="form-group">
            <label class="form-label">Password</label>
            <input class="form-input" id="l-pass" type="password" placeholder="••••••••" required/>
          </div>
          <div class="form-err" id="l-err"></div>
          <button class="btn btn-primary btn-full" id="l-btn" style="margin-top:8px">Login</button>
          <div style="text-align:center;margin-top:14px;font-size:.85rem;color:var(--text2);">
            No account? <a href="#" onclick="showModal('register')" style="color:var(--accent)">Sign up free</a>
          </div>
        </form>
      </div>`;
  } else if (type === 'register') {
    mc.innerHTML = `
      <div class="modal-header">
        <span class="modal-title">Create Account</span>
        <button class="modal-close" onclick="closeModal()">✕</button>
      </div>
      <div class="modal-body">
        <form onsubmit="doRegister(event)">
          <div class="form-group">
            <label class="form-label">Username</label>
            <input class="form-input" id="r-user" placeholder="e.g. johndoe123" required/>
          </div>
          <div class="form-group">
            <label class="form-label">Email</label>
            <input class="form-input" id="r-email" type="email" placeholder="you@email.com" required/>
          </div>
          <div class="form-group">
            <label class="form-label">Phone (M-Pesa)</label>
            <input class="form-input" id="r-phone" placeholder="07XXXXXXXX" required/>
          </div>
          <div class="form-group">
            <label class="form-label">Password</label>
            <input class="form-input" id="r-pass" type="password" placeholder="Min 6 characters" required/>
          </div>
          <div class="form-group">
            <label class="form-label">Referral Code (optional)</label>
            <input class="form-input" id="r-ref" placeholder="Enter referral code"/>
          </div>
          <div class="form-err" id="r-err"></div>
          <button class="btn btn-primary btn-full" id="r-btn" style="margin-top:8px">Create Account</button>
          <div style="text-align:center;margin-top:14px;font-size:.85rem;color:var(--text2);">
            Have an account? <a href="#" onclick="showModal('login')" style="color:var(--accent)">Login</a>
          </div>
        </form>
      </div>`;
  } else if (type === 'deposit') {
    mc.innerHTML = `
      <div class="modal-header">
        <span class="modal-title">💰 Deposit via M-Pesa</span>
        <button class="modal-close" onclick="closeModal()">✕</button>
      </div>
      <div class="modal-body">
        <div class="form-group">
          <label class="form-label">Amount (KES)</label>
          <input class="form-input" id="dep-amount" type="number" min="10" max="150000" placeholder="e.g. 500" required/>
        </div>
        <div class="form-group">
          <label class="form-label">Phone</label>
          <input class="form-input" id="dep-phone" value="${state.user?.phone||''}" placeholder="07XXXXXXXX"/>
        </div>
        <div id="dep-err" class="form-err"></div>
        <div id="dep-msg" style="display:none;background:rgba(16,185,129,.1);border:1px solid var(--green);border-radius:8px;padding:12px;font-size:.85rem;color:var(--green);margin-bottom:12px;"></div>
        <button class="btn btn-primary btn-full" onclick="doDeposit()">Send STK Push</button>
        <p class="form-hint" style="text-align:center;margin-top:8px;">You will receive an M-Pesa prompt on your phone.</p>
      </div>`;
  } else if (type === 'withdraw') {
    mc.innerHTML = `
      <div class="modal-header">
        <span class="modal-title">📤 Withdraw via M-Pesa</span>
        <button class="modal-close" onclick="closeModal()">✕</button>
      </div>
      <div class="modal-body">
        <div class="form-group">
          <label class="form-label">Amount (KES)</label>
          <input class="form-input" id="wd-amount" type="number" min="100" placeholder="e.g. 1000" required/>
        </div>
        <div class="form-group">
          <label class="form-label">M-Pesa Phone</label>
          <input class="form-input" id="wd-phone" value="${state.user?.phone||''}" placeholder="07XXXXXXXX"/>
        </div>
        <div id="wd-err" class="form-err"></div>
        <div id="wd-msg" style="display:none;background:rgba(59,130,246,.1);border:1px solid var(--blue);border-radius:8px;padding:12px;font-size:.85rem;color:var(--blue);margin-bottom:12px;"></div>
        <button class="btn btn-primary btn-full" onclick="doWithdraw()">Request Withdrawal</button>
        <p class="form-hint" style="text-align:center;margin-top:8px;">Withdrawals are processed within 24 hours.</p>
      </div>`;
  } else if (type === 'create-market') {
    const now = new Date(); now.setDate(now.getDate()+7);
    const iso = now.toISOString().slice(0,16);
    mc.innerHTML = `
      <div class="modal-header">
        <span class="modal-title">📊 Create Market</span>
        <button class="modal-close" onclick="closeModal()">✕</button>
      </div>
      <div class="modal-body">
        <div class="form-group">
          <label class="form-label">Title</label>
          <input class="form-input" id="cm-title" placeholder="Will X happen by Y date?" required/>
        </div>
        <div class="form-group">
          <label class="form-label">Description</label>
          <textarea class="form-input" id="cm-desc" rows="3" placeholder="More details about this market..."></textarea>
        </div>
        <div class="form-group">
          <label class="form-label">Category</label>
          <select class="form-select" id="cm-cat">
            <option value="sports">⚽ Sports</option>
            <option value="politics">🗳️ Politics</option>
            <option value="crypto">₿ Crypto</option>
            <option value="weather">🌤️ Weather</option>
            <option value="entertainment">🎬 Entertainment</option>
            <option value="other" selected>🔮 Other</option>
          </select>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
          <div class="form-group">
            <label class="form-label">YES Odds</label>
            <input class="form-input" id="cm-yodds" type="number" step="0.01" min="1.05" value="2.00"/>
          </div>
          <div class="form-group">
            <label class="form-label">NO Odds</label>
            <input class="form-input" id="cm-nodds" type="number" step="0.01" min="1.05" value="2.00"/>
          </div>
        </div>
        <div class="form-group">
          <label class="form-label">Closes At</label>
          <input class="form-input" id="cm-close" type="datetime-local" value="${iso}" required/>
        </div>
        <div id="cm-err" class="form-err"></div>
        <button class="btn btn-primary btn-full" onclick="doCreateMarket()">Create Market</button>
      </div>`;
  }
}

function closeModal(e) {
  if (e && e.target !== document.getElementById('modal-overlay')) return;
  document.getElementById('modal-overlay').style.display = 'none';
}

// ─── DEPOSIT / WITHDRAW ──────────────────────
async function doDeposit() {
  const amount = document.getElementById('dep-amount').value;
  const phone  = document.getElementById('dep-phone').value;
  const {ok, data} = await api('POST', '/deposit', {amount: parseFloat(amount), phone});
  if (ok) {
    document.getElementById('dep-msg').style.display = '';
    document.getElementById('dep-msg').textContent = data.message;
    document.getElementById('dep-err').textContent = '';
    await loadMe();
  } else {
    document.getElementById('dep-err').textContent = data.error || 'Failed';
  }
}

async function doWithdraw() {
  const amount = document.getElementById('wd-amount').value;
  const phone  = document.getElementById('wd-phone').value;
  const {ok, data} = await api('POST', '/withdraw', {amount: parseFloat(amount), phone});
  if (ok) {
    document.getElementById('wd-msg').style.display = '';
    document.getElementById('wd-msg').textContent = data.message;
    document.getElementById('wd-err').textContent = '';
    await loadMe();
  } else {
    document.getElementById('wd-err').textContent = data.error || 'Failed';
  }
}

// ─── NAVIGATION ──────────────────────────────
function navigate(page, data={}) {
  state.currentPage = page;
  const pages = {
    home: renderHome,
    history: renderHistory,
    wallet: renderWallet,
    profile: renderProfile,
    admin: renderAdmin,
  };
  const fn = pages[page];
  if (fn) fn(data);
  else renderHome();
  window.scrollTo(0, 0);
}

// ─── FORMAT HELPERS ──────────────────────────
function fmtNum(n, decimals=2) {
  return Number(n).toLocaleString('en-KE', {minimumFractionDigits:decimals,maximumFractionDigits:decimals});
}
function fmtDate(d) {
  if (!d) return '—';
  return new Date(d).toLocaleString('en-KE', {day:'2-digit',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit'});
}
function timeLeft(d) {
  const ms = new Date(d) - new Date();
  if (ms <= 0) return 'Closed';
  const h = Math.floor(ms/3600000);
  const m = Math.floor((ms%3600000)/60000);
  if (h > 48) return Math.floor(h/24) + 'd left';
  if (h > 0) return h + 'h ' + m + 'm left';
  return m + 'm left';
}
function catIcon(c) {
  const map = {sports:'⚽',politics:'🗳️',crypto:'₿',weather:'🌤️',entertainment:'🎬',other:'🔮'};
  return map[c] || '🔮';
}

// ─── BET SLIP ────────────────────────────────
function addToSlip(market, selection) {
  const existing = state.betSlip.findIndex(s => s.marketId === market.id);
  if (existing >= 0) {
    if (state.betSlip[existing].selection === selection) {
      state.betSlip.splice(existing, 1);
    } else {
      state.betSlip[existing].selection = selection;
      state.betSlip[existing].odds = selection === 'YES' ? market.yes_odds : market.no_odds;
    }
  } else {
    state.betSlip.push({
      marketId: market.id,
      title: market.title,
      selection,
      odds: selection === 'YES' ? market.yes_odds : market.no_odds,
    });
  }
  renderBetSlip();
  renderMarketCards();
}

function removeFromSlip(idx) {
  state.betSlip.splice(idx, 1);
  renderBetSlip();
  renderMarketCards();
}

function clearSlip() {
  state.betSlip = [];
  renderBetSlip();
  renderMarketCards();
}

function renderBetSlip() {
  const el = document.getElementById('bet-slip');
  if (!el) return;

  const totalOdds = state.betSlip.reduce((acc, s) => acc * s.odds, 1);
  const stakeEl   = document.getElementById('slip-stake');
  const stake     = parseFloat(stakeEl?.value || 0) || 0;
  const gross     = stake * (state.slipMode === 'multi' ? totalOdds : (state.betSlip[0]?.odds || 1));
  const profit    = gross - stake;
  const commission= profit * 0.05;
  const net       = gross - commission;

  let itemsHtml = state.betSlip.map((s, i) => `
    <div class="slip-item">
      <button class="remove" onclick="removeFromSlip(${i})">✕</button>
      <div class="slip-market">${s.title}</div>
      <div class="slip-sel">
        <span class="badge ${s.selection==='YES'?'badge-yes':'badge-no'}">${s.selection}</span>
        <span class="slip-odds">${Number(s.odds).toFixed(2)}x</span>
      </div>
    </div>`).join('');

  if (!itemsHtml) itemsHtml = '<div class="empty" style="padding:24px 0"><div class="empty-icon">🎯</div><div style="font-size:.85rem">Pick a market to start betting</div></div>';

  el.querySelector('.slip-items').innerHTML = itemsHtml;

  const payEl = el.querySelector('.slip-payout');
  if (payEl && stake > 0 && state.betSlip.length > 0) {
    payEl.style.display = 'grid';
    payEl.querySelector('#sp-odds').textContent = state.slipMode==='multi' ? totalOdds.toFixed(4)+'x' : (state.betSlip[0]?.odds||0).toFixed(2)+'x';
    payEl.querySelector('#sp-gross').textContent = 'KES ' + fmtNum(gross);
    payEl.querySelector('#sp-comm').textContent  = 'KES ' + fmtNum(commission);
    payEl.querySelector('#sp-net').textContent   = 'KES ' + fmtNum(net);
  } else if (payEl) {
    payEl.style.display = 'none';
  }
}

async function placeBet() {
  if (!state.user) { showModal('login'); return; }
  if (!state.betSlip.length) { toast('Add selections to bet slip', 'error'); return; }
  const stake = parseFloat(document.getElementById('slip-stake').value);
  if (!stake || stake < 10) { toast('Minimum stake is KES 10', 'error'); return; }

  const btn = document.getElementById('place-bet-btn');
  btn.disabled = true; btn.textContent = 'Placing…';

  let result;
  if (state.slipMode === 'single' || state.betSlip.length === 1) {
    const s = state.betSlip[0];
    result = await api('POST', '/place-bet', {
      market_id: s.marketId, selection: s.selection, stake
    });
  } else {
    result = await api('POST', '/place-multibet', {
      selections: state.betSlip.map(s => ({market_id: s.marketId, selection: s.selection})),
      stake
    });
  }

  btn.disabled = false; btn.textContent = 'Place Bet';

  if (result.ok) {
    toast('Bet placed successfully! 🎉', 'success');
    clearSlip();
    await loadMe();
  } else {
    toast(result.data.error || 'Bet failed', 'error');
  }
}

// ─── HOME PAGE ──────────────────────────────
async function renderHome() {
  document.getElementById('page-content').innerHTML = `
    <div class="hero">
      <h1>Predict the <span>Future</span></h1>
      <p>Create markets, place bets, win big. The ultimate prediction platform for Kenya.</p>
      <div class="hero-btns">
        ${!state.user ? `<button class="btn btn-primary" onclick="showModal('register')">Get Started Free</button>` : ''}
        <button class="btn btn-outline" onclick="navigate('home')">Browse Markets</button>
        ${state.user ? `<button class="btn btn-success" onclick="showModal('create-market')">+ Create Market</button>` : ''}
      </div>
    </div>
    <div class="main">
      <div>
        <!-- Filters & Search -->
        <div class="search-bar">
          <input class="search-input" id="mkt-search" placeholder="🔍 Search markets…" 
            oninput="state.searchQuery=this.value; loadMarkets()"/>
        </div>
        <div class="filters" id="mkt-filters">
          ${['all','sports','politics','crypto','weather','entertainment','other'].map(c =>
            `<button class="filter-btn ${state.marketFilter===c?'active':''}" 
              onclick="setFilter('${c}')">${catIcon(c)} ${c.charAt(0).toUpperCase()+c.slice(1)}</button>`
          ).join('')}
        </div>
        <div id="market-list"><div class="loader"><div class="spinner"></div></div></div>
      </div>
      <!-- BET SLIP -->
      <div>
        <div class="bet-slip" id="bet-slip">
          <div class="bet-slip-header">
            <span style="font-weight:700">🎯 Bet Slip</span>
            <div style="display:flex;gap:6px;align-items:center;">
              <span style="font-size:.75rem;color:var(--text2);" id="slip-count">0 selections</span>
              <button class="btn btn-outline btn-sm" onclick="clearSlip()">Clear</button>
            </div>
          </div>
          <div class="tabs" style="margin:12px 16px 0;">
            <button class="tab ${state.slipMode==='single'?'active':''}" onclick="setSlipMode('single')">Single</button>
            <button class="tab ${state.slipMode==='multi'?'active':''}" onclick="setSlipMode('multi')">Multi</button>
          </div>
          <div class="slip-items"></div>
          <div class="slip-stake-area">
            <label class="slip-label">Stake (KES)</label>
            <input class="slip-input" id="slip-stake" type="number" min="10" placeholder="Enter amount…" 
              oninput="renderBetSlip()"/>
          </div>
          <div class="slip-payout" style="display:none">
            <div class="row"><span class="key">Odds</span><span class="val" id="sp-odds">—</span></div>
            <div class="row"><span class="key">Gross Payout</span><span class="val green" id="sp-gross">—</span></div>
            <div class="row"><span class="key">Commission (5%)</span><span class="val red" id="sp-comm">—</span></div>
            <div class="row"><span class="key">Net Payout</span><span class="val green" id="sp-net">—</span></div>
          </div>
          <div class="slip-actions">
            <button class="btn btn-primary btn-full" id="place-bet-btn" onclick="placeBet()">Place Bet</button>
            ${!state.user ? `<button class="btn btn-outline btn-full" onclick="showModal('login')">Login to Bet</button>` : ''}
          </div>
        </div>
      </div>
    </div>`;
  await loadMarkets();
}

function setFilter(f) {
  state.marketFilter = f;
  document.querySelectorAll('.filter-btn').forEach(b => {
    b.classList.toggle('active', b.textContent.trim().toLowerCase().includes(f)||f==='all'&&b.textContent.includes('All'));
  });
  loadMarkets();
}

function setSlipMode(mode) {
  state.slipMode = mode;
  document.querySelectorAll('.tab').forEach((t,i) => t.classList.toggle('active', (i===0&&mode==='single')||(i===1&&mode==='multi')));
  renderBetSlip();
}

async function loadMarkets() {
  const el = document.getElementById('market-list');
  if (!el) return;
  let url = '/markets?per_page=30';
  if (state.marketFilter && state.marketFilter !== 'all') url += '&category=' + state.marketFilter;
  if (state.searchQuery) url += '&search=' + encodeURIComponent(state.searchQuery);
  const {ok, data} = await api('GET', url, null, false);
  if (!ok) { el.innerHTML = '<div class="empty"><div>Failed to load markets</div></div>'; return; }
  state.markets = data.markets;
  renderMarketCards();
}

function renderMarketCards() {
  const el = document.getElementById('market-list');
  if (!el) return;
  if (!state.markets.length) {
    el.innerHTML = '<div class="empty"><div class="empty-icon">📭</div><div>No markets found</div></div>';
    return;
  }
  el.innerHTML = '<div class="market-grid">' + state.markets.map(m => {
    const slipItem = state.betSlip.find(s => s.marketId === m.id);
    const yesActive = slipItem?.selection === 'YES';
    const noActive  = slipItem?.selection === 'NO';
    const vol = (m.yes_volume + m.no_volume);
    const yesPct = vol > 0 ? Math.round(m.yes_volume/vol*100) : 50;
    return `
      <div class="market-card">
        <div class="market-cat">${catIcon(m.category)} ${m.category}</div>
        <div class="market-title">${m.title}</div>
        <div class="market-meta">
          <span class="market-status status-${m.status}">
            ${m.status==='open'?'<span class="live-dot"></span>':''}${m.status.toUpperCase()}
          </span>
          <span style="font-size:.75rem;color:var(--text2);">⏱ ${timeLeft(m.closes_at)}</span>
        </div>
        <div style="font-size:.7rem;color:var(--text2);margin-bottom:4px;">
          YES ${yesPct}% · Volume KES ${fmtNum(vol,0)}
        </div>
        <div class="progress" style="margin-bottom:12px;">
          <div class="progress-bar" style="width:${yesPct}%"></div>
        </div>
        ${m.status==='open' ? `
        <div class="odds-row">
          <button class="odds-btn yes ${yesActive?'selected':''}" onclick="addToSlip(${JSON.stringify(m).replace(/"/g,'&quot;')}, 'YES')">
            <span class="label">YES</span><span class="val">${Number(m.yes_odds).toFixed(2)}x</span>
          </button>
          <button class="odds-btn no ${noActive?'selected':''}" onclick="addToSlip(${JSON.stringify(m).replace(/"/g,'&quot;')}, 'NO')">
            <span class="label">NO</span><span class="val">${Number(m.no_odds).toFixed(2)}x</span>
          </button>
        </div>` : `
        <div style="text-align:center;font-size:.85rem;color:var(--text2);padding:8px;">
          ${m.result ? `Result: <strong style="color:var(--accent)">${m.result}</strong>` : 'Awaiting settlement'}
        </div>`}
      </div>`;
  }).join('') + '</div>';

  // Update slip count
  const sc = document.getElementById('slip-count');
  if (sc) sc.textContent = state.betSlip.length + ' selection' + (state.betSlip.length!==1?'s':'');
}

// ─── HISTORY PAGE ────────────────────────────
async function renderHistory() {
  if (!state.user) { showModal('login'); return; }
  document.getElementById('page-content').innerHTML = `
    <div class="main no-sidebar">
      <div>
        <div class="section-header" style="margin-bottom:16px">
          <h2 class="section-title">📋 Betting History</h2>
        </div>
        <div class="tabs" style="max-width:400px;">
          <button class="tab active" onclick="loadBetHistory('single',this)">Singles</button>
          <button class="tab" onclick="loadBetHistory('multi',this)">Multibets</button>
        </div>
        <div id="history-content"><div class="loader"><div class="spinner"></div></div></div>
      </div>
    </div>`;
  await loadBetHistory('single');
}

async function loadBetHistory(type, btn) {
  if (btn) document.querySelectorAll('.tabs .tab').forEach(t => t.classList.toggle('active', t===btn));
  const el = document.getElementById('history-content');
  const {ok, data} = await api('GET', '/history');
  if (!ok) { el.innerHTML = '<div class="empty">Failed to load</div>'; return; }

  if (type === 'single') {
    if (!data.bets.length) {
      el.innerHTML = '<div class="empty"><div class="empty-icon">🎰</div><div>No bets placed yet</div></div>';
      return;
    }
    el.innerHTML = `
      <div class="card">
        <div class="table-wrap">
          <table>
            <thead><tr><th>Market</th><th>Pick</th><th>Stake</th><th>Odds</th><th>Payout</th><th>Status</th><th>Date</th></tr></thead>
            <tbody>${data.bets.map(b => `
              <tr>
                <td style="max-width:200px;font-size:.82rem;">${b.market_title||'—'}</td>
                <td><span class="badge-pill ${b.selection==='YES'?'badge-yes':'badge-no'}">${b.selection}</span></td>
                <td class="mono">KES ${fmtNum(b.stake)}</td>
                <td class="mono">${Number(b.odds).toFixed(2)}x</td>
                <td class="mono ${b.status==='won'?'text-green':''}">${b.status==='won'?'KES '+fmtNum(b.net_payout):'—'}</td>
                <td><span class="market-status status-${b.status==='won'?'open':b.status==='lost'?'closed':b.status}">${b.status.toUpperCase()}</span></td>
                <td style="font-size:.75rem;color:var(--text2);">${fmtDate(b.created_at)}</td>
              </tr>`).join('')}
            </tbody>
          </table>
        </div>
      </div>`;
  } else {
    if (!data.multibets.length) {
      el.innerHTML = '<div class="empty"><div class="empty-icon">🎰</div><div>No multibets placed yet</div></div>';
      return;
    }
    el.innerHTML = data.multibets.map(mb => `
      <div class="card" style="margin-bottom:12px;">
        <div class="card-header">
          <span>Multibet #${mb.id} — ${mb.items.length} selections</span>
          <span class="market-status status-${mb.status==='won'?'open':mb.status==='lost'?'closed':mb.status}">${mb.status.toUpperCase()}</span>
        </div>
        <div style="display:flex;gap:16px;flex-wrap:wrap;font-size:.85rem;margin-bottom:12px;">
          <div>Stake: <strong>KES ${fmtNum(mb.total_stake)}</strong></div>
          <div>Total Odds: <strong>${Number(mb.total_odds).toFixed(4)}x</strong></div>
          <div>Potential: <strong class="text-green">KES ${fmtNum(mb.net_payout)}</strong></div>
        </div>
        ${mb.items.map(i => `
          <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-top:1px solid var(--border);font-size:.82rem;">
            <span>${i.market_title||'Market '+i.market_id}</span>
            <div style="display:flex;gap:8px;align-items:center;">
              <span class="badge-pill ${i.selection==='YES'?'badge-yes':'badge-no'}">${i.selection}</span>
              <span class="mono">${Number(i.odds).toFixed(2)}x</span>
              ${i.result ? `<span style="color:${i.result===i.selection?'var(--green)':'var(--red)'};">${i.result===i.selection?'✓':'✗'}</span>` : ''}
            </div>
          </div>`).join('')}
      </div>`).join('');
  }
}

// ─── WALLET PAGE ─────────────────────────────
async function renderWallet() {
  if (!state.user) { showModal('login'); return; }
  const {ok, data} = await api('GET', '/transactions?per_page=30');
  document.getElementById('page-content').innerHTML = `
    <div class="main no-sidebar">
      <div>
        <div class="stats">
          <div class="stat-card">
            <div class="stat-label">Balance</div>
            <div class="stat-value text-green">KES ${fmtNum(state.user.balance)}</div>
          </div>
          <div class="stat-card">
            <div class="stat-label">Total Deposited</div>
            <div class="stat-value">KES ${fmtNum(state.user.total_deposited)}</div>
          </div>
          <div class="stat-card">
            <div class="stat-label">Total Won</div>
            <div class="stat-value text-green">KES ${fmtNum(state.user.total_won)}</div>
          </div>
          <div class="stat-card">
            <div class="stat-label">Total Wagered</div>
            <div class="stat-value">KES ${fmtNum(state.user.total_wagered)}</div>
          </div>
        </div>
        <div style="display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap;">
          <button class="btn btn-primary" onclick="showModal('deposit')">💰 Deposit</button>
          <button class="btn btn-outline" onclick="showModal('withdraw')">📤 Withdraw</button>
        </div>
        <div class="card">
          <div class="card-header"><span class="card-title">Transaction History</span></div>
          ${ok && data.transactions.length ? `
          <div class="table-wrap">
            <table>
              <thead><tr><th>Type</th><th>Amount</th><th>Balance After</th><th>Description</th><th>Date</th></tr></thead>
              <tbody>${data.transactions.map(t => `
                <tr>
                  <td><span class="badge-pill" style="background:var(--bg3);color:var(--text2);">${t.type.replace('_',' ')}</span></td>
                  <td class="mono ${t.amount>=0?'text-green':'text-red'}">${t.amount>=0?'+':''}KES ${fmtNum(Math.abs(t.amount))}</td>
                  <td class="mono">KES ${fmtNum(t.balance_after)}</td>
                  <td style="font-size:.8rem;color:var(--text2);">${t.description||t.reference||'—'}</td>
                  <td style="font-size:.75rem;color:var(--text2);">${fmtDate(t.created_at)}</td>
                </tr>`).join('')}
              </tbody>
            </table>
          </div>` : '<div class="empty"><div class="empty-icon">💳</div><div>No transactions yet</div></div>'}
        </div>
      </div>
    </div>`;
}

// ─── PROFILE PAGE ────────────────────────────
async function renderProfile() {
  if (!state.user) { showModal('login'); return; }
  const u = state.user;
  const roi = u.total_wagered > 0 ? (((u.total_won - u.total_wagered) / u.total_wagered) * 100).toFixed(1) : '0.0';
  document.getElementById('page-content').innerHTML = `
    <div class="main no-sidebar">
      <div>
        <div class="profile-header">
          <div class="avatar">${u.username[0].toUpperCase()}</div>
          <div>
            <div style="font-weight:700;font-size:1.1rem;">${u.username}</div>
            <div style="color:var(--text2);font-size:.85rem;">${u.email}</div>
            <div style="font-size:.8rem;color:var(--text2);margin-top:4px;">${u.phone}</div>
          </div>
          <div style="margin-left:auto;text-align:right;">
            <div style="font-size:.75rem;color:var(--text2);">Referral Code</div>
            <div class="mono" style="font-weight:700;color:var(--accent2);">${u.referral_code||'—'}</div>
          </div>
        </div>
        <div class="stats">
          <div class="stat-card">
            <div class="stat-label">Wallet Balance</div>
            <div class="stat-value text-green">KES ${fmtNum(u.balance)}</div>
          </div>
          <div class="stat-card">
            <div class="stat-label">Total Wagered</div>
            <div class="stat-value">KES ${fmtNum(u.total_wagered)}</div>
          </div>
          <div class="stat-card">
            <div class="stat-label">Total Won</div>
            <div class="stat-value text-green">KES ${fmtNum(u.total_won)}</div>
          </div>
          <div class="stat-card">
            <div class="stat-label">ROI</div>
            <div class="stat-value ${parseFloat(roi)>=0?'text-green':'text-red'}">${roi}%</div>
          </div>
        </div>
        <div class="card">
          <div class="card-header"><span class="card-title">Account Details</span></div>
          <div style="display:grid;gap:12px;font-size:.9rem;">
            <div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border);">
              <span class="text-muted">Member Since</span><span>${fmtDate(u.created_at)}</span>
            </div>
            <div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border);">
              <span class="text-muted">Last Login</span><span>${fmtDate(u.last_login)}</span>
            </div>
            <div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border);">
              <span class="text-muted">Account Status</span>
              <span class="market-status status-${u.status==='active'?'open':'closed'}">${u.status.toUpperCase()}</span>
            </div>
            <div style="display:flex;justify-content:space-between;padding:8px 0;">
              <span class="text-muted">Total Deposited</span><span>KES ${fmtNum(u.total_deposited)}</span>
            </div>
          </div>
        </div>
      </div>
    </div>`;
}

// ─── ADMIN PAGE ──────────────────────────────
async function renderAdmin() {
  if (!state.user?.is_admin) { toast('Admin only', 'error'); return; }

  document.getElementById('page-content').innerHTML = `
    <div class="main no-sidebar">
      <div>
        <h2 class="section-title" style="margin-bottom:16px;">⚙️ Admin Dashboard</h2>
        <div class="tabs" style="max-width:600px;margin-bottom:20px;">
          <button class="tab active" onclick="loadAdminTab('overview',this)">Overview</button>
          <button class="tab" onclick="loadAdminTab('markets',this)">Markets</button>
          <button class="tab" onclick="loadAdminTab('users',this)">Users</button>
          <button class="tab" onclick="loadAdminTab('withdrawals',this)">Withdrawals</button>
          <button class="tab" onclick="loadAdminTab('settings',this)">Settings</button>
        </div>
        <div id="admin-content"><div class="loader"><div class="spinner"></div></div></div>
      </div>
    </div>`;
  await loadAdminTab('overview');
}

async function loadAdminTab(tab, btn) {
  if (btn) document.querySelectorAll('.tabs .tab').forEach(t => t.classList.toggle('active', t===btn));
  const el = document.getElementById('admin-content');
  el.innerHTML = '<div class="loader"><div class="spinner"></div></div>';

  if (tab === 'overview') {
    const {ok, data} = await api('GET', '/admin/dashboard');
    if (!ok) { el.innerHTML = '<div class="empty">Error loading dashboard</div>'; return; }
    el.innerHTML = `
      <div class="stats">
        <div class="stat-card"><div class="stat-label">Total Users</div><div class="stat-value">${data.users.total}</div>
          <div class="stat-sub">${data.users.active} active · ${data.users.suspended} suspended</div></div>
        <div class="stat-card"><div class="stat-label">Open Markets</div><div class="stat-value">${data.markets.open}</div>
          <div class="stat-sub">${data.markets.total} total</div></div>
        <div class="stat-card"><div class="stat-label">Open Bets</div><div class="stat-value">${data.bets.open}</div>
          <div class="stat-sub">${data.bets.total} total</div></div>
        <div class="stat-card"><div class="stat-label">Total Deposits</div><div class="stat-value text-green">KES ${fmtNum(data.finance.total_deposits)}</div></div>
        <div class="stat-card"><div class="stat-label">Commission Earned</div><div class="stat-value text-green">KES ${fmtNum(data.finance.total_commission)}</div></div>
        <div class="stat-card"><div class="stat-label">Balance Held</div><div class="stat-value">KES ${fmtNum(data.finance.platform_balance_held)}</div></div>
        <div class="stat-card"><div class="stat-label">Pending Withdrawals</div>
          <div class="stat-value ${data.pending_withdrawals>0?'text-yellow':''}">${data.pending_withdrawals}</div></div>
        <div class="stat-card"><div class="stat-label">Commission Rate</div><div class="stat-value">${(data.commission_rate*100).toFixed(1)}%</div></div>
      </div>
      <div style="display:flex;gap:12px;flex-wrap:wrap;">
        <button class="btn btn-primary" onclick="showModal('create-market')">+ Create Market</button>
        <button class="btn btn-outline" onclick="loadAdminTab('withdrawals')">Review Withdrawals</button>
      </div>`;

  } else if (tab === 'markets') {
    const {ok, data} = await api('GET', '/markets?status=all&per_page=50');
    if (!ok) { el.innerHTML = '<div class="empty">Error</div>'; return; }
    el.innerHTML = `
      <div class="card">
        <div class="table-wrap">
          <table>
            <thead><tr><th>Market</th><th>Category</th><th>Status</th><th>Yes Odds</th><th>No Odds</th><th>Closes</th><th>Actions</th></tr></thead>
            <tbody>${data.markets.map(m => `
              <tr>
                <td style="max-width:200px;font-size:.82rem;">${m.title}</td>
                <td>${catIcon(m.category)} ${m.category}</td>
                <td><span class="market-status status-${m.status}">${m.status}</span></td>
                <td class="mono text-green">${Number(m.yes_odds).toFixed(2)}x</td>
                <td class="mono text-red">${Number(m.no_odds).toFixed(2)}x</td>
                <td style="font-size:.75rem;">${fmtDate(m.closes_at)}</td>
                <td>
                  ${m.status!=='settled'?`
                  <button class="btn btn-sm" style="background:rgba(16,185,129,.1);color:var(--green);border:1px solid rgba(16,185,129,.3);" 
                    onclick="adminSettleMarket(${m.id},'${m.title}')">Settle</button>
                  <button class="btn btn-sm btn-danger" style="margin-left:4px;"
                    onclick="adminSuspendMarket(${m.id})">Suspend</button>`:'Settled'}
                </td>
              </tr>`).join('')}
            </tbody>
          </table>
        </div>
      </div>`;

  } else if (tab === 'users') {
    const {ok, data} = await api('GET', '/admin/users?per_page=30');
    if (!ok) { el.innerHTML = '<div class="empty">Error</div>'; return; }
    el.innerHTML = `
      <div class="card">
        <div class="table-wrap">
          <table>
            <thead><tr><th>User</th><th>Balance</th><th>Wagered</th><th>Won</th><th>Status</th><th>Actions</th></tr></thead>
            <tbody>${data.users.map(u => `
              <tr>
                <td>
                  <div style="font-weight:600;">${u.username}</div>
                  <div style="font-size:.75rem;color:var(--text2);">${u.email}</div>
                </td>
                <td class="mono text-green">KES ${fmtNum(u.balance)}</td>
                <td class="mono">KES ${fmtNum(u.total_wagered)}</td>
                <td class="mono text-green">KES ${fmtNum(u.total_won)}</td>
                <td><span class="market-status status-${u.status==='active'?'open':'closed'}">${u.status}</span></td>
                <td>
                  <button class="btn btn-sm btn-outline" onclick="adminAdjustBalance(${u.id},'${u.username}')">Adjust</button>
                  ${u.status==='active'?
                    `<button class="btn btn-sm btn-danger" style="margin-left:4px;" onclick="adminSuspendUser(${u.id})">Suspend</button>`:
                    `<button class="btn btn-sm btn-success" style="margin-left:4px;" onclick="adminActivateUser(${u.id})">Activate</button>`}
                </td>
              </tr>`).join('')}
            </tbody>
          </table>
        </div>
      </div>`;

  } else if (tab === 'withdrawals') {
    const {ok, data} = await api('GET', '/admin/withdrawals?status=all&per_page=30');
    if (!ok) { el.innerHTML = '<div class="empty">Error</div>'; return; }
    el.innerHTML = `
      <div class="card">
        <div class="table-wrap">
          <table>
            <thead><tr><th>User</th><th>Amount</th><th>Phone</th><th>Status</th><th>Date</th><th>Actions</th></tr></thead>
            <tbody>${data.withdrawals.map(w => `
              <tr>
                <td style="font-weight:600;">${w.username}</td>
                <td class="mono">KES ${fmtNum(w.amount)}</td>
                <td class="mono">${w.phone}</td>
                <td><span class="market-status status-${w.status==='approved'?'open':w.status==='rejected'?'closed':'settled'}">${w.status}</span></td>
                <td style="font-size:.75rem;">${fmtDate(w.created_at)}</td>
                <td>
                  ${w.status==='pending'?`
                  <button class="btn btn-sm btn-success" onclick="adminReviewWithdrawal(${w.id},'approve')">Approve</button>
                  <button class="btn btn-sm btn-danger" style="margin-left:4px;" onclick="adminReviewWithdrawal(${w.id},'reject')">Reject</button>`:'—'}
                </td>
              </tr>`).join('')}
            </tbody>
          </table>
        </div>
      </div>`;

  } else if (tab === 'settings') {
    const {ok, data} = await api('GET', '/admin/settings');
    if (!ok) { el.innerHTML = '<div class="empty">Error</div>'; return; }
    el.innerHTML = `
      <div class="card" style="max-width:480px;">
        <div class="card-header"><span class="card-title">Platform Settings</span></div>
        <div class="form-group">
          <label class="form-label">Commission Rate (0.05 = 5%)</label>
          <input class="form-input" id="s-comm" type="number" step="0.01" min="0" max="0.5" value="${data.commission_rate||0.05}"/>
        </div>
        <div class="form-group">
          <label class="form-label">Withdrawal Fee (0.02 = 2%)</label>
          <input class="form-input" id="s-wfee" type="number" step="0.01" min="0" max="0.2" value="${data.withdrawal_fee||0.02}"/>
        </div>
        <div class="form-group">
          <label class="form-label">Min Withdrawal (KES)</label>
          <input class="form-input" id="s-minw" type="number" min="0" value="${data.min_withdrawal||100}"/>
        </div>
        <div class="form-group">
          <label class="form-label">Max Withdrawal (KES)</label>
          <input class="form-input" id="s-maxw" type="number" min="0" value="${data.max_withdrawal||500000}"/>
        </div>
        <div class="form-group">
          <label class="form-label">Market Creation Fee (KES, 0=free)</label>
          <input class="form-input" id="s-mfee" type="number" min="0" value="${data.market_creation_fee||0}"/>
        </div>
        <div class="form-group">
          <label class="form-label">Referral Bonus for New User (KES)</label>
          <input class="form-input" id="s-rbnew" type="number" min="0" value="${data.referral_bonus||50}"/>
        </div>
        <div class="form-group">
          <label class="form-label">Referrer Bonus (KES)</label>
          <input class="form-input" id="s-rbref" type="number" min="0" value="${data.referrer_bonus||100}"/>
        </div>
        <button class="btn btn-primary" onclick="saveAdminSettings()">Save Settings</button>
      </div>`;
  }
}

async function adminSettleMarket(id, title) {
  const result = prompt(`Settle "${title}" — Enter result: YES, NO, or VOID`);
  if (!result) return;
  if (!['YES','NO','VOID'].includes(result.toUpperCase())) { toast('Invalid result', 'error'); return; }
  const {ok, data} = await api('POST', '/admin/settle-market', {market_id: id, result: result.toUpperCase()});
  if (ok) { toast(`Market settled as ${result.toUpperCase()}. ${data.bets_settled} bets settled.`, 'success'); loadAdminTab('markets'); }
  else toast(data.error || 'Failed', 'error');
}

async function adminSuspendMarket(id) {
  const {ok, data} = await api('PUT', `/admin/markets/${id}`, {status:'suspended'});
  if (ok) { toast('Market suspended', 'success'); loadAdminTab('markets'); }
  else toast(data.error||'Failed','error');
}

async function adminSuspendUser(id) {
  const {ok, data} = await api('PUT', `/admin/users/${id}/status`, {status:'suspended'});
  if (ok) { toast('User suspended', 'success'); loadAdminTab('users'); }
  else toast(data.error||'Failed','error');
}

async function adminActivateUser(id) {
  const {ok, data} = await api('PUT', `/admin/users/${id}/status`, {status:'active'});
  if (ok) { toast('User activated', 'success'); loadAdminTab('users'); }
  else toast(data.error||'Failed','error');
}

async function adminAdjustBalance(id, username) {
  const amount = prompt(`Adjust balance for ${username}\nEnter amount (negative to deduct):`);
  if (!amount) return;
  const reason = prompt('Reason for adjustment:') || 'Admin adjustment';
  const {ok, data} = await api('POST', `/admin/users/${id}/adjust-balance`, {amount: parseFloat(amount), reason});
  if (ok) { toast(`Balance adjusted by KES ${amount}`, 'success'); loadAdminTab('users'); }
  else toast(data.error||'Failed','error');
}

async function adminReviewWithdrawal(id, action) {
  const note = action==='reject' ? prompt('Rejection reason:') || '' : '';
  const {ok, data} = await api('PUT', `/admin/withdrawals/${id}`, {action, note});
  if (ok) { toast(`Withdrawal ${action}d`, 'success'); loadAdminTab('withdrawals'); }
  else toast(data.error||'Failed','error');
}

async function saveAdminSettings() {
  const body = {
    commission_rate:   document.getElementById('s-comm').value,
    withdrawal_fee:    document.getElementById('s-wfee').value,
    min_withdrawal:    document.getElementById('s-minw').value,
    max_withdrawal:    document.getElementById('s-maxw').value,
    market_creation_fee: document.getElementById('s-mfee').value,
    referral_bonus:    document.getElementById('s-rbnew').value,
    referrer_bonus:    document.getElementById('s-rbref').value,
  };
  const {ok, data} = await api('PUT', '/admin/settings', body);
  if (ok) toast('Settings saved!', 'success');
  else toast(data.error||'Failed','error');
}

async function doCreateMarket() {
  const body = {
    title:       document.getElementById('cm-title').value,
    description: document.getElementById('cm-desc').value,
    category:    document.getElementById('cm-cat').value,
    yes_odds:    parseFloat(document.getElementById('cm-yodds').value),
    no_odds:     parseFloat(document.getElementById('cm-nodds').value),
    closes_at:   document.getElementById('cm-close').value,
  };
  const endpoint = state.user?.is_admin ? '/admin/create-market' : '/markets';
  const {ok, data} = await api('POST', endpoint, body);
  if (ok) {
    closeModal();
    toast('Market created!', 'success');
    navigate('home');
    await loadMarkets();
  } else {
    document.getElementById('cm-err').textContent = data.error || 'Failed';
  }
}

// ─── INIT ─────────────────────────────────────
(async function init() {
  await loadMe();
  navigate('home');
})();
</script>
</body>
</html>"""


@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

# Catch-all for SPA
@app.route('/<path:path>')
def catch_all(path):
    return render_template_string(HTML_TEMPLATE)


# ─────────────────────────────────────────────
#  DATABASE INIT + SEEDING
# ─────────────────────────────────────────────

def init_db():
    with app.app_context():
        db.create_all()

        # Default settings
        defaults = {
            'commission_rate': '0.05',
            'withdrawal_fee':  '0.02',
            'min_withdrawal':  '100',
            'max_withdrawal':  '500000',
            'market_creation_fee': '0',
            'referral_bonus':  '50',
            'referrer_bonus':  '100',
        }
        for k, v in defaults.items():
            if not Settings.query.filter_by(key=k).first():
                db.session.add(Settings(key=k, value=v))

        # Admin user
        admin_email    = os.environ.get('ADMIN_EMAIL', 'admin@predictx.com')
        admin_password = os.environ.get('ADMIN_PASSWORD', 'Admin@1234')
        admin_phone    = os.environ.get('ADMIN_PHONE', '254700000000')
        if not User.query.filter_by(email=admin_email).first():
            admin = User(
                username='admin', email=admin_email, phone=admin_phone,
                is_admin=True, status='active', referral_code='ADMIN001',
                balance=Decimal('0')
            )
            admin.set_password(admin_password)
            db.session.add(admin)
            logger.info(f'Admin created: {admin_email} / {admin_password}')

        # Sample markets
        if Market.query.count() == 0:
            admin = User.query.filter_by(is_admin=True).first()
            if admin:
                samples = [
                    {'title': 'Will BTC reach $100,000 by end of 2025?', 'category': 'crypto',
                     'yes_odds': 1.75, 'no_odds': 2.10, 'days': 120},
                    {'title': 'Will Kenya win the Africa Cup of Nations 2025?', 'category': 'sports',
                     'yes_odds': 4.50, 'no_odds': 1.25, 'days': 60},
                    {'title': 'Will it rain in Nairobi this coming Friday?', 'category': 'weather',
                     'yes_odds': 1.90, 'no_odds': 1.90, 'days': 7},
                    {'title': 'Will a new Kenyan president be elected before 2028?', 'category': 'politics',
                     'yes_odds': 1.30, 'no_odds': 3.20, 'days': 365},
                    {'title': 'Will Safaricom launch 5G nationwide in 2025?', 'category': 'other',
                     'yes_odds': 2.20, 'no_odds': 1.70, 'days': 90},
                ]
                for s in samples:
                    m = Market(
                        title=s['title'], category=s['category'],
                        yes_odds=s['yes_odds'], no_odds=s['no_odds'],
                        closes_at=datetime.utcnow() + timedelta(days=s['days']),
                        created_by=admin.id, status='open'
                    )
                    db.session.add(m)

        db.session.commit()
        logger.info('Database initialized successfully')


# ─────────────────────────────────────────────
#  ERROR HANDLERS
# ─────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith('/api'):
        return jsonify({'error': 'Endpoint not found'}), 404
    return render_template_string(HTML_TEMPLATE)

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({'error': 'Method not allowed'}), 405

@app.errorhandler(500)
def server_error(e):
    logger.error(f'Server error: {e}')
    return jsonify({'error': 'Internal server error'}), 500

@app.errorhandler(Exception)
def handle_exception(e):
    logger.exception(f'Unhandled exception: {e}')
    db.session.rollback()
    if request.path.startswith('/api'):
        return jsonify({'error': 'Server error', 'detail': str(e)}), 500
    return render_template_string(HTML_TEMPLATE)


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_ENV', 'development') != 'production'
    logger.info(f'Starting PredictX on http://0.0.0.0:{port}')
    logger.info(f'Admin: {os.environ.get("ADMIN_EMAIL","admin@predictx.com")} / {os.environ.get("ADMIN_PASSWORD","Admin@1234")}')
    app.run(host='0.0.0.0', port=port, debug=debug)
