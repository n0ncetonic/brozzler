/*
 * brozzler/js-templates/classUmbrabehavior.js.j2 - an umbra/brozzler behavior class
 *
 * Copyright (C) 2017 Internet Archive
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */


class UmbraBehavior {

    constructor(actions) {
        this.IDLE_TIMEOUT_SEC = 10;
        this.actions = actions;
        this.alreadyDone = [];
        this.idleSince = null;
        this.intervalId = null;
        this.intervalTimeMs = 250;
        this.state = null; // currently unused
        this.index = 0;
    }

    simpleIntervalFunc() {
        var k = this.index;
        var selector = this.actions[k].selector;
        var action = this.actions[k].do ? this.actions[k].do : 'click';
        var closeSelector = this.actions[k].closeSelector ? this.actions[k].closeSelector : null;

        // var limit = this.actions[k].limit ? this.actions[k].limit : 0;
        // if (limit && !(this.actions[k].alreadyDone)) {
        //     this.actions[k].alreadyDone = [];
        // }

        // if (limit && this.actions[k].alreadyDone && this.actions[k].alreadyDone.length >= limit) {
        //     continue;
        // }

        var didSomething = false;
        var somethingLeftBelow = false;
        var somethingLeftAbove = false;

        var iframes = document.querySelectorAll("iframe");
        var iframesLength = iframes.length;
        var documents = Array(iframesLength + 1);
        documents[0] = document;
        for (var i = 0; i < iframesLength; i++) {
            documents[i+1] = iframes[i].contentWindow.document;
        }
        var documentsLength = documents.length;

        for (var j = 0; j < documentsLength; j++) {
            if (closeSelector) {
                var closeTargets = documents[j].querySelectorAll(closeSelector);
                if (closeTargets != []) {
                    this.doTarget(closeTargets[0], 'click');
                    didSomething = true;
                }
            }

            var doTargets = documents[j].querySelectorAll(selector);
            if (doTargets == []) {
                continue;
            }
            var doTargetsLength = doTargets.length;
            for ( var i = 0; i < doTargetsLength; i++) {
                if (this.alreadyDone.indexOf(doTargets[i]) > -1) {
                    continue;
                }
                if (!this.isVisible(doTargets[i])) {
                    continue;
                }
                // if using limits...
                // if (limit && this.actions[k].alreadyDone && this.actions[k].alreadyDone.length >= limit) {
                //     break;
                // }

                var where = this.aboveBelowOrOnScreen(doTargets[i]);
                if (where == 0) {
                    this.doTarget(doTargets[i], action);
                    // if using limits...
                    // if (this.actions[k].alreadyDone) {
                    //     this.actions[k].alreadyDone.push(doTargets[i]);
                    // }
                    didSomething = true;
                    break; // break from doTargets loop, but not from documents loop
                } else if (where > 0) {
                    somethingLeftBelow = true;
                } else if (where < 0) {
                    somethingLeftAbove = true;
                }
            }
        }
        if (!didSomething) {
            if (somethingLeftAbove) {
                // console.log("scrolling up");
                window.scrollBy(0, -500);
                this.idleSince = null;
            } else if (somethingLeftBelow) {
                // console.log("scrolling"); document.body.clientHeight =+ document.body.clientHeight);
                window.scrollBy(0, 200);
                this.idleSince = null;
            } else if (window.scrollY + window.innerHeight < document.documentElement.scrollHeight) {
                // console.log("scrolling because we're not to the bottom yet");
                window.scrollBy(0, 200);
                this.idleSince = null;
            } else if (this.idleSince == null) {
                this.idleSince = Date.now();
            }
        }
        if (!this.idleSince) {
            this.idleSince = Date.now();
        } else {
            var idleTimeMs = Date.now() - this.idleSince;
            if ((idleTimeMs / 1000) > (this.IDLE_TIMEOUT_SEC - 1) && this.index < (this.actions.length - 1)) {
                console.log("ready for next action");
                this.index += 1;
                this.idleSince = null;
                return;
            }
        }
    }

    aboveBelowOrOnScreen(elem) {
        var eTop = elem.getBoundingClientRect().top;
        if (eTop < window.scrollY) {
            return -1; // above
        } else if (eTop > window.scrollY + window.innerHeight) {
            return 1; // below
        } else {
            return 0; // on screen
        }
    }

    isVisible(elem) {
        return !!(elem.offsetWidth || elem.offsetHeight || elem.getClientRects().length);
    }

    doTarget(target, action) {
        // console.log("doing " + action + target.outerHTML);
        // do mouse over event on target
        // since some urls are requsted only on
        // this event - see
        // https://webarchive.jira.com/browse/AITFIVE-451
        var mouseOverEvent = document.createEvent("Events");
        mouseOverEvent.initEvent("mouseover", true, false);
        target.dispatchEvent(mouseOverEvent);

        if (action == "click") {
            target.click();
        } // add new do's here!

        this.alreadyDone.push(target);
        this.idleSince = null;
    }

    start() {
        var that = this;
        this.intervalId = setInterval(function() {
            that.simpleIntervalFunc()
        }, this.intervalTimeMs);
    }

    isFinished() {
        if (this.idleSince != null) {
            var idleTimeMs = Date.now() - this.idleSince;
            if (idleTimeMs / 1000 > this.IDLE_TIMEOUT_SEC) {
                clearInterval(this.intervalId);
                return true;
            }
        }
        return false;
    }
}

var umbraBehavior = new UmbraBehavior( {{actions|json}} );

// var umbraBehavior = new UmbraBehavior( [{'selector': 'div.teaser, li.pager__item a'}] );

// Called from outside of this script.
var umbraBehaviorFinished = function() {
    return umbraBehavior.isFinished();
};

umbraBehavior.start();
