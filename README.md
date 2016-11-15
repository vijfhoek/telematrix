# telematrix

A bridge between Telegram and [Matrix](http://matrix.org/). Currently under development — this project isn't considered to be in a usable state right now.

## Installation
### Dependencies

First, create a virtualenv and activate it:

```bash
virtualenv venv -p $(which python3)
. venv/bin/activate
```

Then install the requirements using pip:

```bash
pip install -r requirements.txt
```

### Configuration
**telematrix configuration**

First, copy config.json.example to config.json. Then fill in the fields:

* `tokens.hs`: A randomly generated token
* `tokens.as`: Another randomly generated token
* `tokens.telegram`: The Telegram bot API token, as generated by @BotFather
* `tokens.google`: A Google API key, used for URL shortening. Can be left out to disable.
* `hosts.internal`: The homeserver host to connect to internally.
* `hosts.external`: The external domain of the homeserver, used for generating URLs.
* `hosts.bare`: Just the (sub)domain of the server.
* `user_id_format`: A Python `str.format`-style string to format user IDs as
* `db_url`: A SQLAlchemy URL for the database. See the [SQLAlchemy docs](http://docs.sqlalchemy.org/en/latest/core/engines.html).

**Synapse configuration**

Copy asconfig.yaml.example to asconfig.yaml, then fill in the fields:

* `url`: The host and port of telematrix. Most likely `http://localhost:5000`.
* `as_token`: `token.as` from telematrix config.
* `hs_token`: `token.hs` from telematrix config.

The rest of the config can be left as is.

## Contributions

Want to help? Awesome! This bridge still needs a lot of work, so any help is welcome.

A great start is reporting bugs — if you find it doesn't work like it's supposed to, do submit an issue on Github. Or, if you're a programmer (which you probably are, considering you are on this website), feel free to try to fix it yourself. Just make sure Pylint approves of your code!
