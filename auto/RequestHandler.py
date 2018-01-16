#!/usr/bin/env python2

"""
Handle requests to update repositories, using SafeUpdater.
Requests are files in the requests directory (see format in templates).
They are processed lexicographically and deleted once handled.
The script watches the directory forever.

A request may trigger:
- Cloning of tasks on the server as non-bare repositories.
- Task generation, using TaskSandbox.
- Updating of a contest, using cmsImportContest.
"""

from datetime import timedelta
import logging
import os
import sys
from time import sleep
import traceback
import yaml
import pyinotify
import flufl.lock

# This is for pretty logging.
import cms.log

from server_utils.config import REQUESTS_DIR, REQUEST_COOLING, \
    ACTIVE_CONTESTS, CLONE_DIR
from server_utils.auto.SafeUpdater import SafeUpdater


logger = logging.getLogger(__name__)

# These constants control the lock of the requests directory
# (not the repository lock). These are expected to move quickly
# because the only use is writing and reading small files.
_requests_lock_lifetime = timedelta(seconds=3)
_requests_lock_timeout = timedelta(seconds=10)


class RequestHandler(pyinotify.ProcessEvent):
    """
    Class to handle requests in a given directory.
    """

    def my_init(self, _dir=REQUESTS_DIR, contests=ACTIVE_CONTESTS):
        """
        Create a new handler for the given directory.
        If the directory does not exist, raise an exception.

        The given contest paths are taken to be the active contests:
        they will be automatically updated when requests related to them
        are given.

        This method is called by ProcessEvent class, see pyinotify
        documentation.
        """
        super(RequestHandler, self).__init__()

        logger.info("Initializing RequestHandler for directory %s", _dir)
        self.dir = os.path.abspath(_dir)
        self.contests = set(contests)

        if not os.path.isdir(_dir):
            raise Exception("Directory not found: %s" % _dir)

    def watch_forever(self):
        """
        Watch the directory for new files forever.
        This method blocks.
        """
        # See pyinotify documentation.
        # We use self as the EventHandler.
        wm = pyinotify.WatchManager()
        mask = pyinotify.IN_CREATE
        notifier = pyinotify.Notifier(wm, self)
        wm.add_watch(self.dir, mask)
        logger.info("Watching directory forever...")
        notifier.loop()

    def process_IN_CLOSE_WRITE(self, event):
        """
        Handle the event of a new request being created. Note we need its
        content, so we must wait for "IN_CLOSE_WRITE" rather than "IN_CREATE".
        Invokes handle_existing_requests.
        Prints errors on failure.
        """
        request_name = os.path.basename(event.pathname)
        logger.info("Received new request: %s", request_name)
        self.handle_existing_requests()

    def handle_existing_requests(self):
        """
        Invoke handle_request for each file in the requests directory.
        If the directory is empty, does nothing.

        Prints errors on failure.
        """

        logger.info("Going to handle all existing requests.")

        # Get all files, convert to full paths, filter by existence, and sort.
        files_list = os.listdir(self.dir)
        files_list = [os.path.join(self.dir, name) for name in files_list]
        files_list = filter(os.path.isfile, files_list)
        files_list.sort()

        logger.info("Found %s files to handle.", len(files_list))

        for (index, path) in enumerate(files_list):
            success = self.handle_request(path)
            if not success:
                logger.error("Failed to handle %s", os.path.basename(path))
            self._delete_request(path)

            logger.info("Finished %s out of %s requests.",
                        index + 1, len(files_list))
            logger.info("Going to sleep for %s seconds.", REQUEST_COOLING)
            sleep(REQUEST_COOLING)
            logger.info("Woke up.")

    def handle_request(self, request_path):
        """
        Handle a given request file path.

        Prints errors on failures.
        """

        logger.info("Starting to handle %s", os.path.basename(request_path))

        try:
            request = self._get_request_content(request_path)
        except Exception:
            logger.error("Error while opening %s\n%s",
                         request_path, traceback.format_exc())
            return False

        try:
            RequestHandler._validate_request(request)
        except Exception:
            logger.error("Error while validating %s\n%s",
                         request_path, traceback.format_exc())
            return False

        self._act(request)
        return True

    def _act(self, request):
        """
        Perform the actions on a given request, which is assumed to be valid.
        Prints errors on failure.
        """

        # Every repository starts with a subdirectory "tasks", "contests",
        # or "users". This is its type.
        repo = request["repo"]
        repo_type = repo.split("/")[0]

        # Try to update safely. SafeUpdater uses a flufl lock in the
        # repositories directory to avoid race conditions.
        try:
            with SafeUpdater() as updater:
                self._update_safely(updater, repo, repo_type)
        except Exception:
            logger.error("Error while acting on %s\n%s",
                         repo, traceback.format_exc())

    def _update_safely(self, updater, repo, repo_type):
        """
        Process an update safely with the given SafeUpdater.

        Tasks and contests that are not related to the active contests
        are ignored. When an active contest is given, it is updated via
        SafeUpdater.update_contest, and its users are updated as well.

        When a task inside an active contest is given, it is generated
        via SafeUpdater.generate_task, and then its contest is updated.

        When the users repository is given, users are updated via
        SafeUpdater.update_users, and then all active contests as well.

        Raise an exception on failure.
        """

        # Contests are updated only if they are active.
        if repo_type == "contests":
            if repo in self.contests:
                logger.info("Updating contest %s...", repo)
                updater.update_contest(repo, update=True, generate_new=True,
                                       update_users=True)
                logger.info("Updated contest %s", repo)
            else:
                logger.warning("Not updating contest %s "
                               "because it is not active.", repo)
            return

        if repo_type == "users":
            logger.info("Updating users repository...")
            updater.update_users()
            logger.info("Updated users repository.")
            logger.info("Updating all contests to reflect the changed users.")
            for contest in self.contests:
                logger.info("Updating contest %s...", contest)
                # Since we have just updated the users, we don't need to
                # do it again for each contest.
                updater.update_contest(contest, update=True, generate_new=True,
                                       update_users=False)
                logger.info("Updated contest %s", contest)
            logger.info("Finished updating users and contests.")
            return

        # Tasks trigger generation and contest updates only if they
        # are in the active contests. We fetch all such tasks.
        if repo_type == "tasks":
            logger.info("Checking whether task %s is in an active contest.",
                        repo)
            task_contests = self._get_task_contests(repo)
            if not task_contests:
                logger.info("Skipping task %s, it is not active.", repo)
                return
            logger.info("Task %s is active. Generating...", repo)
            updater.generate_task(repo, update=True, allow_clone=True)
            logger.info("Finished generating task %s", repo)
            logger.info("Updating the task's contests...")
            for contest in task_contests:
                logger.info("Updating contest %s...", contest)
                updater.update_contest(contest, update=True, generate_new=True,
                                       update_users=False)
                logger.info("Finished updating contest %s", contest)
            logger.info("Finished updating task %s and its contests.", repo)
            return

        # Unknown repository type.
        raise Exception("Unknown repository type: %s" % repo_type)

    def _get_task_contests(self, task_repo):
        """
        Return a list of all active contests that contain the given task.
        This reads the repository base, so it must be called from within
        a "with SafeUpdater" block.

        Assumes all contests have been cloned.
        Raise an exception on failure.
        """

        result = []
        for contest in self.contests:
            module_path = os.path.join(CLONE_DIR, contest, "module.yaml")
            with open(module_path) as stream:
                contest_params = yaml.safe_load(stream)
            for task in contest_params["tasks"]:
                if task["path"] == task_repo:
                    result += [contest]
        return result

    def _delete_request(self, path):
        """
        Delete the given request. Prints errors on failure.

        If trying to delete something that isn't in the requests directory,
        shut down the program.
        """
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            logger.error("Deleting request %s failed, it is not a file.", path)
            return

        # Sanity check before deleting - make sure we are only deleting
        # content from the requests directory.
        if not path.startswith(self.dir):
            logger.critical("Won't delete: %s", path)
            sys.exit(1)

        try:
            os.remove(path)
        except Exception:
            logger.error("Deleting request %s failed:\n%s",
                         path, traceback.format_exc())

    def _get_request_content(self, path):
        """
        Return a dictionary with the content of the given YAML file path.
        Locks the request directory while loading.
        Raise an exception on failure.
        """

        lock_path = os.path.join(self.dir, ".lock")
        lock = flufl.lock.Lock(lock_path, lifetime=_requests_lock_lifetime)

        # This may raise a TimeoutError, which the caller will handle.
        lock.lock(timeout=_requests_lock_timeout)

        # Try returning the file content. Unlock in any case.
        try:
            with open(path) as stream:
                return yaml.safe_load(stream)
        finally:
            lock.unlock()

    @staticmethod
    def _validate_request(request):
        """
        Check whether the given request content is valid.
        Raise an exception if not.
        """

        if not isinstance(request, dict):
            raise Exception("Expected request to be a dictionary, "
                            "but it is %s" % type(request))

        if "user" not in request:
            raise Exception("Not found 'user' key in request.")

        user = request["user"]
        if not isinstance(user, basestring):
            raise Exception("Expected user to be a string, "
                            "but it is %s" % type(user))

        if "repo" not in request:
            raise Exception("Not found 'repo' key in request.")

        repo = request["repo"]
        if not isinstance(repo, basestring):
            raise Exception("Expected repo to be a string, "
                            "but it is %s" % type(repo))

        repo_type = repo.split("/")[0]
        if repo_type not in ("tasks", "contests", "users"):
            raise Exception("Expected repository type to be 'tasks', "
                            "'contests', or 'users': %s" % repo)


def main():
    """
    Handle all existing requests that may have accumulated,
    then listen for requests forever.
    """
    handler = RequestHandler()
    handler.watch_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())