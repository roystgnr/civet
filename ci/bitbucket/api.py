
# Copyright 2016 Battelle Energy Alliance, LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from django.core.urlresolvers import reverse
import logging
import json
from ci.git_api import GitAPI, GitException
import traceback

logger = logging.getLogger('ci')

class BitBucketAPI(GitAPI):
    STATUS = ((GitAPI.PENDING, "INPROGRESS"),
        (GitAPI.ERROR, "FAILED"),
        (GitAPI.SUCCESS, "SUCCESSFUL"),
        (GitAPI.FAILURE, "FAILED"),
        (GitAPI.RUNNING, "INPROGRESS"),
        (GitAPI.CANCELED, "STOPPED"),
        )

    def __init__(self, config):
        super(BitBucketAPI, self).__init__()
        self._config = config
        self._api2_url = config.get("api2_url", "")
        self._api1_url = config.get("api1_url", "")
        self._bitbucket_url = config.get("html_url", "")
        self._request_timeout = config.get("request_timeout", 5)
        self._install_webhook = config.get("install_webhook", False)
        self._update_remote = config.get("remote_update", False)
        self._prefix = "%s_" % config["hostname"]
        self._repos_key = "%s_repos" % self._prefix
        self._org_repos_key = "%s_org_repos" % self._prefix
        self._user_key = "%s_user" % self._prefix

    def sign_in_url(self):
        return reverse('ci:bitbucket:sign_in', args=[self._config["hostname"]])

    def user_url(self):
        return "%s/user" % self._api1_url

    def repos_url(self, affiliation=None):
        return '{}/user/repositories'.format(self._api1_url)

    def repo_url(self, owner, repo):
        return "%s/repositories/%s/%s" % (self._api1_url, owner, repo)

    def branches_url(self, owner, repo):
        return "%s/branches" % (self.repo_url(owner, repo))

    def repo_html_url(self, owner, repo):
        return "%s/%s/%s" % (self._bitbucket_url, owner, repo)

    def pr_html_url(self, owner, repo, pr_id):
        return "%s/pull-requests/%s" % (self.repo_html_url(owner, repo), pr_id)

    def branch_html_url(self, owner, repo, branch):
        return "%s/branches/%s" % (self.repo_html_url(owner, repo), branch)

    def git_url(self, owner, repo):
        return "git@bitbucket.org:%s/%s" % (owner, repo)

    def commit_html_url(self, owner, repo, sha):
        return "%s/commits/%s" % (self.repo_html_url(owner, repo), sha)

    def pr_comment_api_url(self, owner, repo, pr_id):
        return "%s/pullrequests/%s/comments" % (self.repo_url(owner, repo), pr_id)

    def commit_comment_url(self, owner, repo, sha):
        return self.commit_html_url(owner, repo, sha)

    def collaborator_url(self, owner):
        return "%s/repositories/%s" % (self._api2_url, owner)

    def get_all_repos(self, auth_session, owner):
        owner_repos, org_repos = self.get_user_repos(auth_session, owner)
        owner_repos.extend(org_repos)
        return owner_repos

    def get_user_repos(self, auth_session, username):
        if not username:
            return [], []

        response = auth_session.get(self.repos_url())
        data = self.get_all_pages(auth_session, response)
        owner_repo = []
        org_repos = []
        if 'message' not in data:
            for repo in data:
                owner = repo['owner']
                name = repo['name']
                full_name = "{}/{}".format(owner, name)
                if owner == username:
                    owner_repo.append(full_name)
                else:
                    org_repos.append(full_name)
            org_repos.sort()
            owner_repo.sort()
            logger.debug('Org repos: {}'.format(org_repos))
            logger.debug('Repos repos: {}'.format(owner_repo))
        return owner_repo, org_repos

    def get_repos(self, auth_session, session):
        if self._repos_key in session and self._org_repos_key in session:
            return session[self._repos_key]

        user = session.get(self._user_key)
        owner_repos, org_repos = self.get_user_repos(auth_session, user)
        session[self._org_repos_key] = org_repos
        session[self._repos_key] = owner_repos
        return owner_repos

    def get_branches(self, auth_session, owner, repo):
        response = auth_session.get(self.branches_url(owner, repo))
        data = self.get_all_pages(auth_session, response)
        if response.status_code == 200:
            return data.keys()
        return []

    def get_org_repos(self, auth_session, session):
        if self._org_repos_key in session:
            return session[self._org_repos_key]
        self.get_repos(auth_session, session)
        org_repos = session.get(self._org_repos_key)
        if org_repos:
            return org_repos
        return []

    def update_pr_status(self, oauth_session, base, head, state, event_url, description, context, job_stage):
        """
        FIXME: BitBucket has statuses but haven't figured out how to make them work.
        """

    def is_collaborator(self, oauth_session, user, repo):
        # first just check to see if the user is the owner
        if repo.user == user:
            return True
        # now ask bitbucket
        url = self.collaborator_url(repo.user.name)
        logger.debug('Checking %s' % url)
        response = oauth_session.get(url, data={'role': 'contributor'})
        data = self.get_all_pages(oauth_session, response)
        if response.status_code == 200:
            for repo_data in data['values']:
                if repo_data['name'] == repo.name:
                    logger.debug('User %s is a collaborator on %s' % (user, repo))
                    return True
        logger.debug('User %s is not a collaborator on %s' % (user, repo))
        return False

    def pr_review_comment(self, oauth_session, url, msg):
        """
        FIXME: Need to implement
        """

    def pr_comment(self, oauth_session, url, msg):
        """
        Add a comment on a PR
        """
        if not self._update_remote:
            return

        try:
            data = {'content': msg}
            logger.info('POSTing to {}: {}'.format(url, msg))
            response = oauth_session.post(url, data=data)
            if response.status_code != 200:
                logger.warning('Bad response when posting to {}: {}'.format(url, response.content))
        except Exception as e:
            logger.warning("Failed to leave comment.\nComment: %s\nError: %s" %(msg, traceback.format_exc(e)))

    def get_all_pages(self, oauth_session, response):
        all_json = response.json()
        while 'next' in response.links:
            response = oauth_session.get(response.links['next']['url'])
            all_json.extend(response.json())
        return all_json

    def last_sha(self, oauth_session, owner, repo, branch):
        url = self.branches_url(owner, repo)
        try:
            response = oauth_session.get(url)
            data = self.get_all_pages(oauth_session, response)
            response.raise_for_status()
            branch_data = data.get(branch)
            if branch_data:
                return branch_data['raw_node']
        except Exception as e:
            logger.warning("Failed to get branch information at %s.\nError: %s" % (url, traceback.format_exc(e)))

    def install_webhooks(self, request, auth_session, user, repo):
        if not self._install_webhook:
            return

        hook_url = '{}/repositories/{}/{}/hooks'.format(self._api2_url, repo.user.name, repo.name)
        callback_url = request.build_absolute_uri(reverse('ci:bitbucket:webhook', args=[user.build_key]))
        response = auth_session.get(hook_url)
        data = self.get_all_pages(auth_session, response)
        have_hook = False
        for hook in data['values']:
            if 'pullrequest:created' not in hook['events'] or 'repo:push' not in hook['events']:
                continue
            if hook['url'] == callback_url:
                have_hook = True
                break

        if have_hook:
            logger.debug('Webhook already exists')
            return None

        add_hook = {
            'description': 'CIVET webook',
            'url': callback_url,
            'active': True,
            'events': [
                'repo:push',
                'pullrequest:created',
                'pullrequest:updated',
                'pullrequest:approved',
                'pullrequest:rejected',
                'pullrequest:fulfilled',
                ],
            }
        response = auth_session.post(hook_url, data=add_hook)
        data = response.json()
        if response.status_code != 201:
            logger.debug('data: {}'.format(json.dumps(data, indent=2)))
            raise GitException(data)
        logger.debug('Added webhook to %s for user %s' % (repo, user.name))

    def is_member(self, oauth, team, user):
        logger.warning("FIXME: BitBucket function not implemented: is_member")
        return False

    def add_pr_label(self, builduser, repo, pr_num, label_name):
        logger.warning("FIXME: BitBucket function not implemented: add_pr_label")

    def remove_pr_label(self, builduser, repo, pr_num, label_name):
        logger.warning("FIXME: BitBucket function not implemented: remove_pr_label")

    def get_pr_comments(self, oauth, url, username, comment_re):
        logger.warning("FIXME: BitBucket function not implemented: get_pr_comments")
        return []

    def remove_pr_comment(self, oauth, comment):
        logger.warning("FIXME: BitBucket function not implemented: remove_pr_comment")

    def edit_pr_comment(self, oauth, comment, msg):
        logger.warning("FIXME: BitBucket function not implemented: edit_pr_comment")

    def get_open_prs(self, oauth_session, owner, repo):
        url = "%s/repositories/%s/%s/pullrequests" % (self._api2_url, owner, repo)
        params = {"state": "OPEN"}
        try:
            response = oauth_session.get(url, params=params)
            data = self.get_all_pages(oauth_session, response)
            response.raise_for_status()
            open_prs = []
            for pr in data.get("values", []):
                open_prs.append({"number": pr["id"], "title": pr["title"], "html_url": pr["links"]["html"]})
            return open_prs
        except Exception as e:
            logger.warning("Failed to get open PRs for %s/%s at URL: %s\nError: %s" % (owner, repo, url, e))
