# coding: utf8

from __future__ import division, print_function, unicode_literals

from datetime import timedelta
from email.utils import parsedate
from time import gmtime

from six.moves.http_cookies import SimpleCookie

from babel.messages.catalog import Message
from pando.utils import utcnow

from liberapay.constants import SESSION
from liberapay.models.participant import Participant
from liberapay.testing import postgres_readonly
from liberapay.testing.emails import EmailHarness
from liberapay.utils.i18n import LOCALES


password = 'password'

good_data = {
    'sign-in.username': 'bob',
    'sign-in.password': password,
    'sign-in.email': 'bob@example.com',
}


class TestLogIn(EmailHarness):

    def log_in(self, username, password, url='/sign-in', extra={}, **kw):
        data = {'log-in.id': username, 'log-in.password': password}
        data.update(extra)
        return self.client.POST(url, data, raise_immediately=False, **kw)

    def log_in_and_check(self, p, password, **kw):
        r = self.log_in(p.username, password, **kw)
        return self.check_login(r, p)

    def check_login(self, r, p):
        p = p.refetch()
        # Basic checks
        assert r.code == 302
        expected = str('%s:%s') % (p.id, p.session_token)
        sess_cookie = r.headers.cookie[SESSION]
        assert sess_cookie.value == expected
        expires = sess_cookie[str('expires')]
        assert expires.endswith(' GMT')
        assert parsedate(expires) > gmtime()
        # More thorough check
        self.check_with_about_me(p.username, r.headers.cookie)
        return p

    def check_with_about_me(self, username, cookies):
        r = self.client.GET('/about/me/', cookies=cookies, raise_immediately=False)
        assert r.code == 302
        assert r.headers[b'Location'] == b'/' + username.encode() + b'/'

    def test_log_in(self):
        alice = self.make_participant('alice')
        alice.update_password(password)
        self.log_in_and_check(alice, password)

    def test_log_in_form_repost(self):
        alice = self.make_participant('alice')
        alice.update_password(password)
        extra = {'name': 'python', 'lang': 'mul', 'form.repost': 'true'}
        r = self.log_in('alice', password, url='/for/new', extra=extra)
        assert r.code == 302
        assert r.headers[b'Location'] == b'/for/python/edit'

    def test_log_in_with_email_as_id(self):
        email = 'alice@example.net'
        alice = self.make_participant('alice')
        alice.add_email(email)
        bob = self.make_participant('bob', email=email)
        bob.update_password(password)
        r = self.log_in(email, password)
        self.check_login(r, bob)

    def test_log_in_with_old_session(self):
        alice = self.make_participant('alice')
        alice.update_session('x', utcnow() - timedelta(days=1))
        alice.authenticated = True
        cookies = SimpleCookie()
        alice.sign_in(cookies)
        print(cookies)
        self.check_with_about_me('alice', cookies)

    def test_log_in_switch_user(self):
        alice = self.make_participant('alice')
        alice.update_password(password)
        bob = self.make_participant('bob')
        bob.authenticated = True
        cookies = SimpleCookie()
        bob.sign_in(cookies)
        self.log_in_and_check(alice, password, cookies=cookies)

    def test_log_in_closed_account(self):
        alice = self.make_participant('alice')
        alice.update_password(password)
        alice.update_status('closed')
        alice2 = self.log_in_and_check(alice, password)
        assert alice2.status == 'active'
        assert alice2.join_time == alice.join_time

    def test_log_in_bad_username(self):
        r = self.log_in('alice', 'password')
        assert SESSION not in r.headers.cookie

    def test_log_in_no_password(self):
        stub = self.make_stub()
        r = self.log_in(stub.username, '')
        assert SESSION not in r.headers.cookie

    def test_log_in_bad_password(self):
        alice = self.make_participant('alice')
        alice.update_password('password')
        r = self.log_in('alice', 'deadbeef')
        assert SESSION not in r.headers.cookie

    def test_log_in_non_ascii_password(self):
        password = 'le blé pousse dans le champ'
        alice = self.make_participant('alice')
        alice.update_password(password)
        self.log_in_and_check(alice, password.encode('utf8'))

    def test_email_login(self):
        email = 'alice@example.net'
        alice = self.make_participant('alice')
        alice.add_email(email)
        alice.close(None)

        data = {'log-in.id': email.upper()}
        r = self.client.POST('/', data, raise_immediately=False)
        alice = alice.refetch()
        assert alice.session_token not in r.headers.raw.decode('ascii')
        assert alice.session_token not in r.body.decode('utf8')

        Participant.dequeue_emails()
        last_email = self.get_last_email()
        assert last_email and last_email['subject'] == 'Log in to Liberapay'
        assert 'log-in.token='+alice.session_token in last_email['text']

        url = '/alice/?foo=bar&log-in.id=%s&log-in.token=%s'
        r = self.client.GxT(url % (alice.id, alice.session_token))
        alice2 = alice.refetch()
        assert alice2.session_token != alice.session_token
        # ↑ this means that the link is only valid once
        assert r.code == 302
        assert r.headers[b'Location'] == b'http://localhost/alice/?foo=bar'
        # ↑ checks that original path and query are preserved

        # Check that we can change our password
        password = 'New-Password'
        r = self.client.POST(
            '/alice/settings/edit',
            {'new-password': password},
            cookies=r.headers.cookie,
            raise_immediately=False,
        )
        assert r.code == 302
        alice2 = Participant.authenticate('id', 'password', alice.id, password)
        assert alice2 and alice2 == alice

    def test_email_login_bad_email(self):
        data = {'log-in.id': 'unknown@example.org'}
        r = self.client.POST('/sign-in', data, raise_immediately=False)
        assert r.code != 302
        assert SESSION not in r.headers.cookie
        Participant.dequeue_emails()
        assert not self.get_emails()

    def test_email_login_bad_id(self):
        r = self.client.GxT('/?log-in.id=1&log-in.token=x')
        assert r.code == 400

    def test_email_login_bad_token(self):
        alice = self.make_participant('alice')
        r = self.client.GxT('/?log-in.id=%s&log-in.token=x' % alice.id)
        assert r.code == 400

    def test_email_login_team_account(self):
        email = 'team@example.net'
        self.make_participant('team', email=email, kind='group')
        data = {'log-in.id': email}
        r = self.client.POST('/log-in', data, raise_immediately=False)
        assert SESSION not in r.headers.cookie
        Participant.dequeue_emails()
        assert not self.get_emails()


class TestSignIn(EmailHarness):

    def sign_in(self, custom={}, extra={}, url='/sign-in', **kw):
        data = dict(good_data, **extra)
        for k, v in custom.items():
            if v is None:
                del data['sign-in.'+k]
                continue
            data['sign-in.'+k] = v
        kw.setdefault('raise_immediately', False)
        return self.client.POST(url, data, **kw)

    def test_sign_in(self):
        fake_msg = Message('Email address verification - Liberapay', 'Vous avez du pain ?')
        LOCALES['fr'].catalog[fake_msg.id].string = fake_msg.string
        r = self.sign_in(HTTP_ACCEPT_LANGUAGE='fr')
        assert r.code == 302, r.text
        assert SESSION in r.headers.cookie
        # Check that an email was sent, in the user's preferred language
        Participant.dequeue_emails()
        last_email = self.get_last_email()
        username = good_data['sign-in.username']
        assert last_email['subject'] == fake_msg.string
        # Check that the new user has an avatar
        p = Participant.from_username(username)
        assert p.avatar_url

    def test_sign_in_form_repost(self):
        extra = {'name': 'python', 'lang': 'mul', 'form.repost': 'true'}
        r = self.sign_in(url='/for/new', extra=extra)
        assert r.code == 302
        assert r.headers[b'Location'] == b'/for/python/edit'

    def test_sign_in_without_username(self):
        r = self.sign_in(dict(username=''))
        assert r.code == 302

    def test_sign_in_non_ascii_username(self):
        r = self.sign_in(dict(username='mélodie'.encode('utf8')))
        assert r.code == 400

    def test_sign_in_non_ascii_password(self):
        r = self.sign_in(dict(password='super clé'.encode('utf8')))
        assert r.code == 302

    def test_sign_in_long_username(self):
        r = self.sign_in(dict(username='a'*200))
        assert r.code == 400

    def test_sign_in_restricted_username(self):
        r = self.sign_in(dict(username='about'))
        assert r.code == 400

    def test_sign_in_without_password(self):
        r = self.sign_in(dict(password=''))
        assert r.code == 302

    def test_sign_in_short_password(self):
        r = self.sign_in(dict(password='a'))
        assert r.code == 400

    def test_sign_in_long_password(self):
        r = self.sign_in(dict(password='a'*200))
        assert r.code == 400

    def test_sign_in_bad_kind(self):
        r = self.sign_in(dict(kind='group'))
        assert r.code == 400

    def test_sign_in_bad_email(self):
        r = self.sign_in(dict(email='foo@bar'))
        assert r.code == 400

    def test_sign_in_without_csrf_cookie(self):
        r = self.sign_in(csrf_token=None)
        assert r.code == 403
        assert "Bad CSRF cookie" in r.text
        assert SESSION not in r.headers.cookie

    def test_sign_in_when_db_is_read_only(self):
        with postgres_readonly(self.db):
            r = self.sign_in(HTTP_ACCEPT=b'text/html')
            assert r.code == 503, r.text
            assert 'read-only' in r.text
