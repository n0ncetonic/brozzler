'''
brozzler/frontier.py - RethinkDbFrontier manages crawl jobs, sites and pages

Copyright (C) 2014-2017 Internet Archive

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
'''

import logging
import brozzler
import random
import time
import datetime
import rethinkdb as r
import doublethink
import urlcanon

class UnexpectedDbResult(Exception):
    pass

class RethinkDbFrontier:
    logger = logging.getLogger(__module__ + "." + __qualname__)

    def __init__(self, rr, shards=None, replicas=None):
        self.rr = rr
        self.shards = shards or len(rr.servers)
        self.replicas = replicas or min(len(rr.servers), 3)
        self._ensure_db()

    def _ensure_db(self):
        dbs = self.rr.db_list().run()
        if not self.rr.dbname in dbs:
            self.logger.info(
                    "creating rethinkdb database %s", repr(self.rr.dbname))
            self.rr.db_create(self.rr.dbname).run()
        tables = self.rr.table_list().run()
        if not "sites" in tables:
            self.logger.info(
                    "creating rethinkdb table 'sites' in database %s",
                    repr(self.rr.dbname))
            self.rr.table_create(
                    "sites", shards=self.shards, replicas=self.replicas).run()
            self.rr.table("sites").index_create("sites_last_disclaimed", [
                r.row["status"], r.row["last_disclaimed"]]).run()
            self.rr.table("sites").index_create("job_id").run()
        if not "pages" in tables:
            self.logger.info(
                    "creating rethinkdb table 'pages' in database %s",
                    repr(self.rr.dbname))
            self.rr.table_create(
                    "pages", shards=self.shards, replicas=self.replicas).run()
            self.rr.table("pages").index_create("priority_by_site", [
                r.row["site_id"], r.row["brozzle_count"],
                r.row["claimed"], r.row["priority"]]).run()
            # this index is for displaying pages in a sensible order in the web
            # console
            self.rr.table("pages").index_create("least_hops", [
                r.row["site_id"], r.row["brozzle_count"],
                r.row["hops_from_seed"]]).run()
        if not "jobs" in tables:
            self.logger.info(
                    "creating rethinkdb table 'jobs' in database %s",
                    repr(self.rr.dbname))
            self.rr.table_create(
                    "jobs", shards=self.shards, replicas=self.replicas).run()

    def _vet_result(self, result, **kwargs):
        # self.logger.debug("vetting expected=%s result=%s", kwargs, result)
        # {'replaced': 0, 'errors': 0, 'skipped': 0, 'inserted': 1, 'deleted': 0, 'generated_keys': ['292859c1-4926-4b27-9d87-b2c367667058'], 'unchanged': 0}
        for k in [
                "replaced", "errors", "skipped", "inserted", "deleted",
                "unchanged"]:
            if k in kwargs:
                expected = kwargs[k]
            else:
                expected = 0
            if isinstance(expected, list):
                if result.get(k) not in kwargs[k]:
                    raise UnexpectedDbResult("expected {} to be one of {} in {}".format(repr(k), expected, result))
            else:
                if result.get(k) != expected:
                    raise UnexpectedDbResult("expected {} to be {} in {}".format(repr(k), expected, result))

    def claim_site(self, worker_id):
        # XXX keep track of aggregate priority and prioritize sites accordingly?
        while True:
            result = (
                    self.rr.table("sites", read_mode="majority")
                    .between(
                        ["ACTIVE", r.minval], ["ACTIVE", r.maxval],
                        index="sites_last_disclaimed")
                    .order_by(index="sites_last_disclaimed")
                    .filter((r.row["claimed"] != True) | (
                        r.row["last_claimed"] < r.now() - 60*60))
                    .limit(1)
                    .update(
                        # try to avoid a race condition resulting in multiple
                        # brozzler-workers claiming the same site
                        # see https://github.com/rethinkdb/rethinkdb/issues/3235#issuecomment-60283038
                        r.branch((r.row["claimed"] != True) | (
                            r.row["last_claimed"] < r.now() - 60*60), {
                                "claimed": True, "last_claimed_by": worker_id,
                                "last_claimed": doublethink.utcnow()}, {}),
                            return_changes=True)).run()
            self._vet_result(result, replaced=[0,1], unchanged=[0,1])
            if result["replaced"] == 1:
                if result["changes"][0]["old_val"]["claimed"]:
                    self.logger.warn(
                            "re-claimed site that was still marked 'claimed' "
                            "because it was last claimed a long time ago "
                            "at %s, and presumably some error stopped it from "
                            "being disclaimed",
                            result["changes"][0]["old_val"]["last_claimed"])
                site = brozzler.Site(self.rr, result["changes"][0]["new_val"])
            else:
                raise brozzler.NothingToClaim
            # XXX This is the only place we enforce time limit for now. Worker
            # loop should probably check time limit. Maybe frontier needs a
            # housekeeping thread to ensure that time limits get enforced in a
            # timely fashion.
            if not self._enforce_time_limit(site):
                return site

    def _enforce_time_limit(self, site):
        if (site.time_limit and site.time_limit > 0
                and site.elapsed() > site.time_limit):
            self.logger.debug(
                    "site FINISHED_TIME_LIMIT! time_limit=%s elapsed=%s %s",
                    site.time_limit, site.elapsed(), site)
            self.finished(site, "FINISHED_TIME_LIMIT")
            return True
        else:
            return False

    def claim_page(self, site, worker_id):
        # ignores the "claimed" field of the page, because only one
        # brozzler-worker can be working on a site at a time, and that would
        # have to be the worker calling this method, so if something is claimed
        # already, it must have been left that way because of some error
        result = self.rr.table("pages").between(
                [site.id, 0, r.minval, r.minval],
                [site.id, 0, r.maxval, r.maxval],
                index="priority_by_site").order_by(
                        index=r.desc("priority_by_site")).limit(
                                1).update({
                                    "claimed":True,
                                    "last_claimed_by":worker_id},
                                    return_changes="always").run()
        self._vet_result(result, unchanged=[0,1], replaced=[0,1])
        if result["unchanged"] == 0 and result["replaced"] == 0:
            raise brozzler.NothingToClaim
        else:
            return brozzler.Page(self.rr, result["changes"][0]["new_val"])

    def has_outstanding_pages(self, site):
        results_iter = self.rr.table("pages").between(
                [site.id, 0, r.minval, r.minval],
                [site.id, 0, r.maxval, r.maxval],
                index="priority_by_site").limit(1).run()
        return len(list(results_iter)) > 0

    def completed_page(self, site, page):
        page.brozzle_count += 1
        page.claimed = False
        # XXX set priority?
        page.save()
        if page.redirect_url and page.hops_from_seed == 0:
            site.note_seed_redirect(page.redirect_url)
            site.save()

    def active_jobs(self):
        results = self.rr.table("jobs").filter({"status":"ACTIVE"}).run()
        for result in results:
            yield brozzler.Job(self.rr, result)

    def honor_stop_request(self, site):
        """Raises brozzler.CrawlStopped if stop has been requested."""
        site.refresh()
        if (site.stop_requested
                and site.stop_requested <= doublethink.utcnow()):
            self.logger.info("stop requested for site %s", site.id)
            raise brozzler.CrawlStopped

        if site.job_id:
            job = brozzler.Job.load(self.rr, site.job_id)
            if (job and job.stop_requested
                    and job.stop_requested <= doublethink.utcnow()):
                self.logger.info("stop requested for job %s", site.job_id)
                raise brozzler.CrawlStopped

    def _maybe_finish_job(self, job_id):
        """Returns True if job is finished."""
        job = brozzler.Job.load(self.rr, job_id)
        if not job:
            return False
        if job.status.startswith("FINISH"):
            self.logger.warn("%s is already %s", job, job.status)
            return True

        results = self.rr.table("sites").get_all(job_id, index="job_id").run()
        n = 0
        for result in results:
            site = brozzler.Site(self.rr, result)
            if not site.status.startswith("FINISH"):
                results.close()
                return False
            n += 1

        self.logger.info(
                "all %s sites finished, job %s is FINISHED!", n, job.id)
        job.finish()
        job.save()
        return True

    def finished(self, site, status):
        self.logger.info("%s %s", status, site)
        site.status = status
        site.claimed = False
        site.last_disclaimed = doublethink.utcnow()
        site.starts_and_stops[-1]["stop"] = doublethink.utcnow()
        site.save()
        if site.job_id:
            self._maybe_finish_job(site.job_id)

    def disclaim_site(self, site, page=None):
        self.logger.info("disclaiming %s", site)
        site.claimed = False
        site.last_disclaimed = doublethink.utcnow()
        if not page and not self.has_outstanding_pages(site):
            self.finished(site, "FINISHED")
        else:
            site.save()
        if page:
            page.claimed = False
            page.save()

    def resume_job(self, job):
        job.status = "ACTIVE"
        job.starts_and_stops.append(
                {"start":doublethink.utcnow(), "stop":None})
        job.save()
        for site in self.job_sites(job.id):
            site.status = "ACTIVE"
            site.starts_and_stops.append(
                    {"start":doublethink.utcnow(), "stop":None})
            site.save()

    def resume_site(self, site):
        if site.job_id:
            # can't call resume_job since that would resume jobs's other sites
            job = brozzler.Job.load(self.rr, site.job_id)
            job.status = "ACTIVE"
            job.starts_and_stops.append(
                    {"start":doublethink.utcnow(), "stop":None})
            job.save()
        site.status = "ACTIVE"
        site.starts_and_stops.append(
                {"start":doublethink.utcnow(), "stop":None})
        site.save()

    def _scope_and_enforce_robots(self, site, parent_page, outlinks):
        '''
        Returns tuple (
            set of in scope urls (uncanonicalized) accepted by robots policy,
            set of in scope urls (canonicalized) blocked by robots policy,
            set of out-of-scope urls (canonicalized)).
        '''
        in_scope = set()
        blocked = set()
        out_of_scope = set()
        for url in outlinks or []:
            url_for_scoping = urlcanon.semantic(url)
            url_for_crawling = urlcanon.whatwg(url)
            urlcanon.canon.remove_fragment(url_for_crawling)
            if site.is_in_scope(url_for_scoping, parent_page=parent_page):
                if brozzler.is_permitted_by_robots(site, str(url_for_crawling)):
                    in_scope.add(url)
                else:
                    blocked.add(str(url_for_crawling))
            else:
                out_of_scope.add(str(url_for_crawling))
        return in_scope, blocked, out_of_scope

    def _build_fresh_pages(self, site, parent_page, urls):
        '''
        Returns a dict of page_id => brozzler.Page.
        '''
        pages = {}
        for url in urls:
            url_for_scoping = urlcanon.semantic(url)
            url_for_crawling = urlcanon.whatwg(url)
            hashtag = (url_for_crawling.hash_sign
                       + url_for_crawling.fragment).decode('utf-8')
            urlcanon.canon.remove_fragment(url_for_crawling)
            if not url_for_scoping.surt().startswith(
                    site.scope['surt'].encode('utf-8')):
                hops_off_surt = parent_page.hops_off_surt + 1
            else:
                hops_off_surt = 0
            page = brozzler.Page(self.rr, {
                'url': str(url_for_crawling),
                'site_id': site.id,
                'job_id': site.job_id,
                'hops_from_seed': parent_page.hops_from_seed + 1,
                'via_page_id': parent_page.id,
                'hops_off_surt': hops_off_surt,
                'hashtags': []})
            if page.id in pages:
                pages[page.id].priority += page.priority
                page = pages[page.id]
            else:
                pages[page.id] = page
            if hashtag:
                page.hashtags = list(set(page.hashtags + [hashtag]))
        return pages

    def scope_and_schedule_outlinks(self, site, parent_page, outlinks):
        decisions = {'accepted':set(),'blocked':set(),'rejected':set()}
        counts = {'added':0,'updated':0,'rejected':0,'blocked':0}

        in_scope, blocked, out_of_scope = self._scope_and_enforce_robots(
                site, parent_page, outlinks)
        decisions['blocked'] = blocked
        decisions['rejected'] = out_of_scope
        counts['blocked'] += len(blocked)
        counts['rejected'] += len(out_of_scope)

        fresh_pages = self._build_fresh_pages(site, parent_page, in_scope)

        # get existing pages from rethinkdb
        results = self.rr.table('pages').get_all(*fresh_pages.keys()).run()
        pages = {doc['id']: brozzler.Page(self.rr, doc) for doc in results}

        # build list of pages to save, consisting of new pages, and existing
        # pages updated with higher priority and new hashtags
        for fresh_page in fresh_pages.values():
            decisions['accepted'].add(fresh_page.url)
            if fresh_page.id in pages:
                page = pages[fresh_page.id]
                page.hashtags = list(set((page.hashtags or [])
                                         + fresh_page.hashtags))
                page.priority += fresh_page.priority
                counts['updated'] += 1
            else:
                pages[fresh_page.id] = fresh_page
                counts['added'] += 1

        result = self.rr.table('pages').insert(
                pages.values(), conflict='replace').run()

        parent_page.outlinks = {}
        for k in decisions:
            parent_page.outlinks[k] = list(decisions[k])
        parent_page.save()

        self.logger.info(
                '%s new links added, %s existing links updated, %s links '
                'rejected, %s links blocked by robots from %s',
                counts['added'], counts['updated'], counts['rejected'],
                counts['blocked'], parent_page)

    def reached_limit(self, site, e):
        self.logger.info("reached_limit site=%s e=%s", site, e)
        assert isinstance(e, brozzler.ReachedLimit)
        if (site.reached_limit
                and site.reached_limit != e.warcprox_meta["reached-limit"]):
            self.logger.warn(
                    "reached limit %s but site had already reached limit %s",
                    e.warcprox_meta["reached-limit"], self.reached_limit)
        else:
            site.reached_limit = e.warcprox_meta["reached-limit"]
            self.finished(site, "FINISHED_REACHED_LIMIT")

    def job_sites(self, job_id):
        results = self.rr.table('sites').get_all(job_id, index="job_id").run()
        for result in results:
            yield brozzler.Site(self.rr, result)

    def seed_page(self, site_id):
        results = self.rr.table("pages").between(
                [site_id, r.minval, r.minval, r.minval],
                [site_id, r.maxval, r.maxval, r.maxval],
                index="priority_by_site").filter({"hops_from_seed":0}).run()
        pages = list(results)
        if len(pages) > 1:
            self.logger.warn(
                    "more than one seed page for site_id %s ?", site_id)
        if len(pages) < 1:
            return None
        return brozzler.Page(self.rr, pages[0])

    def site_pages(self, site_id, brozzled=None):
        '''
        Args:
            site_id (str or int):
            brozzled (bool): if true, results include only pages that have
                been brozzled at least once; if false, only pages that have
                not been brozzled; and if None (the default), all pages
        Returns:
            iterator of brozzler.Page
        '''
        results = self.rr.table("pages").between(
                [site_id, 1 if brozzled is True else 0,
                    r.minval, r.minval],
                [site_id, 0 if brozzled is False else r.maxval,
                    r.maxval, r.maxval],
                index="priority_by_site").run()
        for result in results:
            yield brozzler.Page(self.rr, result)

