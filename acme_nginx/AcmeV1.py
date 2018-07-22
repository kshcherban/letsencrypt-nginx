import base64
import hashlib
import json
import re
import sys
import time
import textwrap
try:
    from urllib.request import urlopen  # Python 3
except ImportError:
    from urllib2 import urlopen  # Python 2
from Acme import Acme


class AcmeV1(Acme):
    def __init__(self, *args, **kwargs):
        """
        Params:
            chain, str, LetsEncrypt Root CA certificate chain
        """
        self.chain = "https://letsencrypt.org/certs/lets-encrypt-x3-cross-signed.pem"
        super(AcmeV1, self).__init__(*args, **kwargs)

    def register_account(self):
        # Generate new 2049 bit account, domain private keys only if not set
        try:
            self.log.info('trying to create account key {0}'.format(self.account_key))
            account_key = self.create_key(self.account_key)
        except Exception as e:
            self.log.error('creating key {0} {1}'.format(type(e).__name__, e))
            sys.exit(1)
        self.log.info('trying to register account key')
        code, result, headers = self.send_signed_request(
            self.api_url + "/acme/new-reg",
            {
                "resource": "new-reg",
                "agreement": "https://letsencrypt.org/documents/LE-SA-v1.2-November-15-2017.pdf"
            })
        if code == 201:
            self.log.info('registered!')
        elif code == 409:
            self.log.info('already registered')
        else:
            self.log.error('error registering: {0} {1} {2}'.format(code, result, headers))
            sys.exit(1)
        return account_key

    def get_certificate(self):
        # Generate new 2049 bit account, domain private keys only if not set
        self.register_account()
        try:
            self.create_key(self.domain_key)
        except Exception as e:
            self.log.error('creating key {0} {1}'.format(type(e).__name__, e))
            sys.exit(1)
        csr = self.create_csr()
        # Solve challenge
        for domain in self.domains:
            self.log.info('requesting challenge')
            code, result, headers = self.send_signed_request(
                self.api_url + "/acme/new-authz",
                {
                    "resource": "new-authz",
                    "identifier": {"type": "dns", "value": domain},
                }
            )
            if code != 201:
                self.log.error(
                    'error requesting challenges: {0} {1}'
                        .format(code, result))
                sys.exit(1)
            challenge = [c for c in json.loads(result.decode('utf8'))['challenges']
                         if c['type'] == "http-01"][0]
            token = re.sub(r"[^A-Za-z0-9_\-]", "_", challenge['token'])
            accountkey_json = json.dumps(
                self._jws()['jwk'],
                sort_keys=True,
                separators=(',', ':'))
            thumbprint = self._b64(
                hashlib.sha256(accountkey_json.encode('utf8')).digest())
            self.log.info('adding nginx virtual host and completing challenge')
            try:
                challenge_dir = self.write_vhost()
                self.write_challenge(challenge_dir, token, thumbprint)
            except Exception as e:
                self.log.error('error adding virtual host {0} {1}'.format(type(e).__name__, e))
                sys.exit(1)
            self.log.info('waiting for challenge verification')
            code, result, headers = self.send_signed_request(
                challenge['uri'],
                {
                    "resource": "challenge",
                    "keyAuthorization": "{0}.{1}".format(token, thumbprint),
                }
            )
            if code != 202:
                self.log.error("error triggering challenge: {0} {1}".format(code, result))
                self._cleanup(
                    ['{0}/{1}'.format(challenge_dir, token), self.vhost],
                    directory=challenge_dir,
                    exit_with_error=True
                )
            self.log.info('waiting for challenge verification')
            while True:
                try:
                    resp = urlopen(challenge['uri'])
                    challenge_status = json.loads(resp.read().decode('utf8'))
                    if challenge_status['status'] == "pending":
                        time.sleep(2)
                    elif challenge_status['status'] == "valid":
                        self.log.info('{0} verified!'.format(domain))
                        break
                    else:
                        self.log.error('{0} challenge did not pass: {1}'.format(domain, challenge_status))
                        self._cleanup(
                            ['{0}/{1}'.format(challenge_dir, token), self.vhost],
                            directory=challenge_dir,
                            exit_with_error=True
                        )
                except IOError as e:
                    self.log.error("error checking challenge: {0} {1}".format(
                        e.code, json.loads(e.read().decode('utf8'))))
            self._cleanup(
                ['{0}/{1}'.format(challenge_dir, token)],
                directory=challenge_dir
            )
        self.log.info('signing certificate')
        code, result, headers = self.send_signed_request(
            self.api_url + "/acme/new-cert",
            {"resource": "new-cert", "csr": self._b64(csr)}
        )
        if code != 201:
            self.log.error("error signing certificate: {0} {1}".format(code, result))
            self._cleanup(
                ['{0}/{1}'.format(challenge_dir, token), self.vhost],
                directory=challenge_dir,
                exit_with_error=True
            )
        self.log.info('certificate signed!')
        try:
            self.log.info('getting chain from {0}'.format(self.chain))
            chain_str = urlopen(self.chain).read()
            if chain_str:
                chain_str = chain_str.decode('utf8')
        except Exception as e:
            self.log.error('error getting chain: {0} {1}'.format(type(e).__name__, e))
            self._cleanup(
                ['{0}/{1}'.format(challenge_dir, token), self.vhost],
                directory=challenge_dir,
                exit_with_error=True
            )
        self.log.info('writing result file in {0}'.format(self.cert_path))
        try:
            with open(self.cert_path, 'w') as fd:
                fd.write(
                    '''-----BEGIN CERTIFICATE-----\n{0}\n-----END CERTIFICATE-----\n'''.format(
                        '\n'.join(textwrap.wrap(
                            base64.b64encode(result).decode('utf8'),
                            64
                        )))
                )
                fd.write(chain_str)
        except Exception as e:
            self.log.error('error writing cert: {0} {1}'.format(type(e).__name__, e))
            sys.exit(1)
        self._cleanup([self.vhost])
        self._reload_nginx()