# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import os
import time
import uuid
import string
import codecs
import logging
import threading

#pylint: disable=import-error
from six.moves.urllib.parse import urlparse, parse_qs
from six.moves.BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer

from . import docs
from .config import TEMPLATES
from .exceptions import InvalidRefreshToken
from .packages.praw.errors import HTTPException, OAuthException
from .packages.praw.handlers import DefaultHandler


_logger = logging.getLogger(__name__)

INDEX = os.path.join(TEMPLATES, 'index.html')


class OAuthHandler(BaseHTTPRequestHandler):

    # params are stored as a global because we don't have control over what
    # gets passed into the handler __init__. These will be accessed by the
    # OAuthHelper class.
    params = {'state': None, 'code': None, 'error': None}
    shutdown_on_request = True

    def do_GET(self):
        """
        Accepts GET requests to http://localhost:6500/, and stores the query
        params in the global dict. If shutdown_on_request is true, stop the
        server after the first successful request.

        The http request may contain the following query params:
            - state : unique identifier, should match what we passed to reddit
            - code  : code that can be exchanged for a refresh token
            - error : if provided, the OAuth error that occurred
        """

        parsed_path = urlparse(self.path)
        if parsed_path.path != '/':
            self.send_error(404)

        qs = parse_qs(parsed_path.query)
        self.params['state'] = qs['state'][0] if 'state' in qs else None
        self.params['code'] = qs['code'][0] if 'code' in qs else None
        self.params['error'] = qs['error'][0] if 'error' in qs else None

        body = self.build_body()

        # send_response also sets the Server and Date headers
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=UTF-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()

        self.wfile.write(body)

        if self.shutdown_on_request:
            # Shutdown the server after serving the request
            # http://stackoverflow.com/a/22533929
            thread = threading.Thread(target=self.server.shutdown)
            thread.daemon = True
            thread.start()

    def log_message(self, format, *args):
        """
        Redirect logging to our own handler instead of stdout
        """
        _logger.debug(format, *args)

    def build_body(self, template_file=INDEX):
        """
        Params:
            template_file (text): Path to an index.html template

        Returns:
            body (bytes): THe utf-8 encoded document body
        """

        if self.params['error'] == 'access_denied':
            message = docs.OAUTH_ACCESS_DENIED
        elif self.params['error'] is not None:
            message = docs.OAUTH_ERROR.format(error=self.params['error'])
        elif self.params['state'] is None or self.params['code'] is None:
            message = docs.OAUTH_INVALID
        else:
            message = docs.OAUTH_SUCCESS

        with codecs.open(template_file, 'r', 'utf-8') as fp:
            index_text = fp.read()

        body = string.Template(index_text).substitute(message=message)
        body = codecs.encode(body, 'utf-8')
        return body


class OAuthHelper(object):

    params = OAuthHandler.params

    def __init__(self, reddit, term, config):

        self.term = term
        self.reddit = reddit
        self.config = config

        # Wait to initialize the server, we don't want to reserve the port
        # unless we know that the server needs to be used.
        self.server = None

        self.reddit.set_oauth_app_info(
            self.config['oauth_client_id'],
            self.config['oauth_client_secret'],
            self.config['oauth_redirect_uri'])

        # Reddit's mobile website works better on terminal browsers
        if not self.term.display:
            if '.compact' not in self.reddit.config.API_PATHS['authorize']:
                self.reddit.config.API_PATHS['authorize'] += '.compact'

    def authorize(self):

        self.params.update(state=None, code=None, error=None)

        # If we already have a token, request new access credentials
        if self.config.refresh_token:
            with self.term.loader('Logging in'):
                try:
                    self.reddit.refresh_access_information(
                        self.config.refresh_token)
                except (HTTPException, OAuthException) as e:
                    # Reddit didn't accept the refresh-token
                    # This appears to throw a generic 400 error instead of the
                    # more specific invalid_token message that it used to send
                    if isinstance(e, HTTPException):
                        if e._raw.status_code != 400:
                            # No special handling if the error is something
                            # temporary like a 5XX.
                            raise e

                    # Otherwise we know the token is bad, so we can remove it.
                    _logger.exception(e)
                    self.clear_oauth_data()
                    raise InvalidRefreshToken(
                        '       Invalid user credentials!\n'
                        'The cached refresh token has been removed')
            return

        state = uuid.uuid4().hex
        authorize_url = self.reddit.get_authorize_url(
            state, scope=self.config['oauth_scope'], refreshable=True)

        if self.server is None:
            address = ('', self.config['oauth_redirect_port'])
            self.server = HTTPServer(address, OAuthHandler)

        if self.term.display:
            # Open a background browser (e.g. firefox) which is non-blocking.
            # The server will block until it responds to its first request,
            # at which point we can check the callback params.
            OAuthHandler.shutdown_on_request = True
            with self.term.loader('Opening browser for authorization'):
                self.term.open_browser(authorize_url)
                self.server.serve_forever()
            if self.term.loader.exception:
                # Don't need to call server.shutdown() because serve_forever()
                # is wrapped in a try-finally that doees it for us.
                return
        else:
            # Open the terminal webbrowser in a background thread and wait
            # while for the user to close the process. Once the process is
            # closed, the iloop is stopped and we can check if the user has
            # hit the callback URL.
            OAuthHandler.shutdown_on_request = False
            with self.term.loader('Redirecting to reddit', delay=0):
                # This load message exists to provide user feedback
                time.sleep(1)

            thread = threading.Thread(target=self.server.serve_forever)
            thread.daemon = True
            thread.start()
            try:
                self.term.open_browser(authorize_url)
            except Exception as e:
                # If an exception is raised it will be seen by the thread
                # so we don't need to explicitly shutdown() the server
                _logger.exception(e)
                self.term.show_notification('Browser Error', style='error')
            else:
                self.server.shutdown()
            finally:
                thread.join()

        if self.params['error'] == 'access_denied':
            self.term.show_notification('Denied access', style='error')
            return
        elif self.params['error']:
            self.term.show_notification('Authentication error', style='error')
            return
        elif self.params['state'] is None:
            # Something went wrong but it's not clear what happened
            return
        elif self.params['state'] != state:
            self.term.show_notification('UUID mismatch', style='error')
            return

        with self.term.loader('Logging in'):
            info = self.reddit.get_access_information(self.params['code'])
        if self.term.loader.exception:
            return

        message = 'Welcome {}!'.format(self.reddit.user.name)
        self.term.show_notification(message)

        self.config.refresh_token = info['refresh_token']
        if self.config['persistent']:
            self.config.save_refresh_token()

    def clear_oauth_data(self):
        self.reddit.clear_authentication()
        self.config.delete_refresh_token()


def fix_cache(func):
    """
    This is a shim around PRAW's 30 second page cache that attempts
    to address broken behavior that hasn't been fixed because PRAW 3
    is deprecated.
    """

    def wraps(self, _cache_key, _cache_ignore, *args, **kwargs):
        if _cache_key:
            # Pop the request's session cookies from the cache key.
            # These appear to be unreliable and change with every
            # request. Also, with the introduction of OAuth I don't think
            # that cookies are being used to store anything that
            # differentiates API requests anyways
            url, items = _cache_key
            _cache_key = (url, (items[0], items[1], items[3], items[4]))

        if kwargs['request'].method != 'GET':
            # Why were POST/PUT/DELETE requests ever being cached???
            _cache_ignore = True

        return func(self, _cache_key, _cache_ignore, *args, **kwargs)
    return wraps


class OAuthRateLimiter(DefaultHandler):
    """Custom PRAW request handler for rate-limiting requests.
    
    This is an alternative to PRAW 3's DefaultHandler that uses
    Reddit's modern API guidelines to rate-limit requests based
    on the X-Ratelimit-* headers returned from Reddit.

    References:
        https://github.com/reddit/reddit/wiki/API
        https://github.com/praw-dev/prawcore/blob/master/prawcore/rate_limit.py
    """

    next_request_timestamp = None

    def delay(self):
        """
        Pause before making the next HTTP request.
        """
        if self.next_request_timestamp is None:
            return

        sleep_seconds = self.next_request_timestamp - time.time()
        if sleep_seconds <= 0:
            return
        time.sleep(sleep_seconds)

    def update(self, response_headers):
        """
        Update the state of the rate limiter based on the response headers:

            X-Ratelimit-Used: Approximate number of requests used this period
            X-Ratelimit-Remaining: Approximate number of requests left to use
            X-Ratelimit-Reset: Approximate number of seconds to end of period

        PRAW 5's rate limiting logic is structured for making hundreds of
        evenly-spaced API requests, which makes sense for running something
        like a bot or crawler.

        This handler's logic, on the other hand, is geared more towards
        interactive usage. It allows for short, sporadic bursts of requests.
        The assumption is that actual users browsing reddit shouldn't ever be
        in danger of hitting the rate limit. If they do hit the limit, they
        will be cutoff until the period resets.
        """

        if 'x-ratelimit-remaining' not in response_headers:
            # This could be because the API returned an error response, or it
            # could be because we're using something like read-only credentials
            # which Reddit doesn't appear to care about rate limiting.
            return

        used = float(response_headers['x-ratelimit-used'])
        remaining = float(response_headers['x-ratelimit-remaining'])
        seconds_to_reset = int(response_headers['x-ratelimit-reset'])
        _logger.debug('Rate limit: %s used, %s remaining, %s reset',
                      used, remaining, seconds_to_reset)

        if remaining <= 0:
            self.next_request_timestamp = time.time() + seconds_to_reset
        else:
            self.next_request_timestamp = None

    @fix_cache
    @DefaultHandler.with_cache
    def request(self, request, proxies, timeout, verify, **_):

        settings = self.http.merge_environment_settings(
            request.url, proxies, False, verify, None)

        self.delay()
        response = self.http.send(
            request, timeout=timeout, allow_redirects=False, **settings)
        self.update(response.headers)
        return response
