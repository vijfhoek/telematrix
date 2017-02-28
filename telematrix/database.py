"""
Defines all database models and provides necessary functions to manage it.
"""
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import sqlalchemy as sa

engine = None
Base = declarative_base()
Session = sessionmaker()
session = None

class ChatLink(Base):
    """Describes a link between the Telegram and Matrix side of the bridge."""
    __tablename__ = 'chat_link'

    id = sa.Column(sa.Integer, primary_key=True)
    matrix_room = sa.Column(sa.String)
    tg_room = sa.Column(sa.BigInteger)
    active = sa.Column(sa.Boolean)

    def __init__(self, matrix_room, tg_room, active):
        self.matrix_room = matrix_room
        self.tg_room = tg_room
        self.active = active


class TgUser(Base):
    """Describes a user on the Telegram side of the bridge."""
    __tablename__ = 'tg_user'

    id = sa.Column(sa.Integer, primary_key=True)
    tg_id = sa.Column(sa.BigInteger)
    name = sa.Column(sa.String)
    profile_pic_id = sa.Column(sa.String)

    def __init__(self, tg_id, name, profile_pic_id):
        self.tg_id = tg_id
        self.name = name
        self.profile_pic_id = profile_pic_id


class MatrixUser(Base):
    """Describes a user on the Matrix side of the bridge."""
    __tablename__ = 'matrix_user'

    id = sa.Column(sa.Integer, primary_key=True)
    matrix_id = sa.Column(sa.String)
    name = sa.Column(sa.String)

    def __init__(self, matrix_id, name):
        self.matrix_id = matrix_id
        self.name = name

class Message(Base):
    """Describes a message in a room bridged between Telegram and Matrix"""
    __tablename__ = "message"

    id = sa.Column(sa.Integer, primary_key=True)
    tg_group_id = sa.Column(sa.BigInteger)
    tg_message_id = sa.Column(sa.BigInteger)

    matrix_room_id = sa.Column(sa.String)
    matrix_event_id = sa.Column(sa.String)

    displayname = sa.Column(sa.String)

    def __init__(self, tg_group_id, tg_message_id, matrix_room_id, matrix_event_id, displayname):
        self.tg_group_id = tg_group_id
        self.tg_message_id = tg_message_id

        self.matrix_room_id = matrix_room_id
        self.matrix_event_id = matrix_event_id

        self.displayname = displayname

def initialize(*args, **kwargs):
    """Initializes the database and creates tables if necessary."""
    global engine, Base, Session, session
    engine = sa.create_engine(*args, **kwargs)
    Session.configure(bind=engine)
    session = Session()
    Base.metadata.bind = engine
    Base.metadata.create_all()
