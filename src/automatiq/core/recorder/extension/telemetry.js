/**
 * Automatiq Telemetry Script
 * Captures clicks, typing, navigation, and generates Chromium DevTools-grade locators.
 */
(function () {
    if (window._automatiqTelemetryLoaded) return;
    window._automatiqTelemetryLoaded = true;

    const _originalConsole = { ...console };
    for (const key in console) {
        if (typeof console[key] === "function") {
            console[key] = function (...args) {
                for (const arg of args) {
                    if (arg instanceof Error) return;
                }
                return _originalConsole[key].apply(this, args);
            };
        }
    }

    const isIframe = window !== window.top;

    // Relay telemetry to the Python ActionServer via the extension's
    // background service worker. A content script shares the *page's* origin
    // for network purposes, so a direct sendBeacon/fetch to http://127.0.0.1
    // is a public->private request that triggers Chromium's Private Network
    // Access permission prompt. The background SW runs under the extension's
    // own origin (with host_permissions) and is exempt from PNA, so it can
    // fetch the loopback endpoint without any prompt.
    function emitAction(payload) {
        payload.url = window.location.href;
        payload.title = document.title;
        payload.is_iframe = isIframe;
        try {
            // Fire-and-forget: the callback exists only to swallow
            // runtime.lastError (e.g. when the SW is mid-wake-up) so Chrome
            // doesn't log an "unchecked runtime.lastError" warning.
            chrome.runtime.sendMessage(payload, () => { void chrome.runtime.lastError; });
        } catch (e) { /* SW not ready / page tearing down — ignore */ }
    }

    emitAction({ type: 'script_loaded', text: 'Telemetry script initialized' });

    // =====================================================================
    // 1. SHARED UTILITIES & SELECTOR GENERATORS (Chromium DevTools Port)
    // =====================================================================
    class SelectorPart {
        constructor(value, optimized) {
            this.value = value;
            this.optimized = optimized || false;
        }
        toString() { return this.value; }
    }

    const findMinMax = ([min, max], fns) => {
        fns.self = fns.self || ((i) => i);
        let index = fns.inc(min), value, isMax;
        do {
            value = fns.valueOf(min);
            isMax = true;
            while (index !== max) {
                min = fns.self(index);
                index = fns.inc(min);
                if (!fns.gte(value, index)) { isMax = false; break; }
            }
        } while (!isMax);
        return value;
    };

    // CSS Selector Generator
    const idSelector = (id) => `#${CSS.escape(id)}`;
    const attributeSelector = (name, value) => `[${name}='${CSS.escape(value)}']`;
    const classSelector = (selector, className) => `${selector}.${CSS.escape(className)}`;
    const nthTypeSelector = (selector, index) => `${selector}:nth-of-type(${index + 1})`;
    const typeSelector = (selector, type) => `${selector}${attributeSelector('type', type)}`;

    const hasUniqueId = (node) => {
        try { return Boolean(node.id) && (node.getRootNode().querySelectorAll(idSelector(node.id)).length === 1); }
        catch (e) { return false; }
    };
    const isUniqueAmongTagNames = (node, children) => {
        for (const child of children) { if (child !== node && child.tagName === node.tagName) return false; }
        return true;
    };
    const isUniqueAmongInputTypes = (node, children) => {
        for (const child of children) { if (child !== node && child.tagName === 'INPUT' && child.type === node.type) return false; }
        return true;
    };
    const getUniqueClassName = (node, children) => {
        const classNames = new Set(node.classList);
        for (const child of children) {
            if (child !== node) {
                for (const className of child.classList) classNames.delete(className);
                if (classNames.size === 0) break;
            }
        }
        if (classNames.size > 0) return classNames.values().next().value;
        return undefined;
    };
    const getTypeIndex = (node, children) => {
        let nthTypeIndex = 0;
        for (const child of children) {
            if (child === node) return nthTypeIndex;
            if (child.tagName === node.tagName) ++nthTypeIndex;
        }
        return 0;
    };

    const getCssSelectorPart = (node, attributes = []) => {
        if (node.nodeType !== Node.ELEMENT_NODE) return;
        for (const attribute of attributes) {
            const value = node.getAttribute(attribute);
            if (value) return new SelectorPart(attributeSelector(attribute, value), true);
        }
        if (hasUniqueId(node)) return new SelectorPart(idSelector(node.id), true);
        const selector = node.tagName.toLowerCase();
        if (['body', 'head', 'html'].includes(selector)) return new SelectorPart(selector, true);
        const parent = node.parentNode;
        if (!parent) return new SelectorPart(selector, true);
        const children = parent.children;
        if (isUniqueAmongTagNames(node, children)) return new SelectorPart(selector, true);
        if (node.tagName === 'INPUT' && isUniqueAmongInputTypes(node, children)) return new SelectorPart(typeSelector(selector, node.type), true);
        const className = getUniqueClassName(node, children);
        if (className !== undefined) return new SelectorPart(classSelector(selector, className), true);
        return new SelectorPart(nthTypeSelector(selector, getTypeIndex(node, children)), false);
    };

    class SelectorRangeOps {
        constructor(attributes = []) { this.buffer = [[]]; this.attributes = attributes; this.depth = 0; }
        inc(node) { return node.parentNode || node.getRootNode(); }
        valueOf(node) {
            const part = getCssSelectorPart(node, this.attributes);
            if (!part) throw new Error('Node is not an element');
            if (this.depth > 1) { this.buffer.unshift([part]); } else { this.buffer[0].unshift(part); }
            this.depth = 0;
            return this.buffer.map(parts => parts.join(' > ')).join(' ');
        }
        gte(selector, node) {
            ++this.depth;
            try { return node.querySelectorAll(selector).length === 1; } catch (e) { return false; }
        }
    }

    const computeCSSSelector = (node, attributes = []) => {
        const selectors = [];
        try {
            let root;
            while (node && node.nodeType === Node.ELEMENT_NODE) {
                root = node.getRootNode();
                selectors.unshift(findMinMax([node, root], new SelectorRangeOps(attributes)));
                node = root.host ? root.host : root;
            }
        } catch (e) { return undefined; }
        return selectors;
    };

    // ARIA Selector Generator
    const browserA11yBindings = {
        getAccessibleName: (node) => node.getAttribute('aria-label') || node.getAttribute('alt') || node.title || node.innerText || '',
        getAccessibleRole: (node) => node.getAttribute('role') || node.localName || ''
    };

    class ARIASelectorComputer {
        constructor(bindings) { this.bindings = bindings; }
        computeUniqueARIASelectorForElements(elements, queryByRoleOnly) {
            const selectors = [];
            let parent = document;
            for (const element of elements) {
                let result = this.queryA11yTreeOneByName(parent, element.name);
                if (result) { selectors.push(element.name); parent = result; continue; }
                if (queryByRoleOnly) {
                    result = this.queryA11yTreeOneByRole(parent, element.role);
                    if (result) { selectors.push(`[role="${element.role}"]`); parent = result; continue; }
                }
                result = this.queryA11yTreeOneByNameAndRole(parent, element.name, element.role);
                if (result) { selectors.push(`${element.name}[role="${element.role}"]`); parent = result; continue; }
                return;
            }
            return selectors;
        }
        queryA11yTreeOneByName(parent, name) {
            if (!name) return null;
            const result = this.queryA11yTree(parent, name, undefined, 2);
            return result.length === 1 ? result[0] : null;
        }
        queryA11yTreeOneByRole(parent, role) {
            if (!role) return null;
            const result = this.queryA11yTree(parent, undefined, role, 2);
            return result.length === 1 ? result[0] : null;
        }
        queryA11yTreeOneByNameAndRole(parent, name, role) {
            if (!role || !name) return null;
            const result = this.queryA11yTree(parent, name, role, 2);
            return result.length === 1 ? result[0] : null;
        }
        queryA11yTree(parent, name, role, maxResults = 0) {
            const result = [];
            if (!name && !role) return result;
            const shouldMatchName = Boolean(name);
            const shouldMatchRole = Boolean(role);
            const collect = (root) => {
                const iter = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
                do {
                    const currentNode = iter.currentNode;
                    if (!currentNode || currentNode.nodeType !== Node.ELEMENT_NODE) continue;
                    if (currentNode.shadowRoot) collect(currentNode.shadowRoot);
                    if (currentNode instanceof ShadowRoot) continue;
                    if (shouldMatchName && this.bindings.getAccessibleName(currentNode) !== name) continue;
                    if (shouldMatchRole && this.bindings.getAccessibleRole(currentNode) !== role) continue;
                    result.push(currentNode);
                    if (maxResults && result.length >= maxResults) return;
                } while (iter.nextNode());
            };
            collect(parent.nodeType === Node.DOCUMENT_NODE ? document.documentElement : parent);
            return result;
        }
        compute(node) {
            let selector, current = node;
            const elements = [];
            while (current) {
                const role = this.bindings.getAccessibleRole(current);
                const name = this.bindings.getAccessibleName(current);
                if (!role && !name) { if (current === node) break; }
                else {
                    elements.unshift({ name, role });
                    selector = this.computeUniqueARIASelectorForElements(elements, current !== node);
                    if (selector) break;
                    if (current !== node) elements.shift();
                }
                current = current.parentNode;
                if (current instanceof ShadowRoot) current = current.host;
            }
            return selector;
        }
    }

    const computeARIASelector = (node) => {
        try { return new ARIASelectorComputer(browserA11yBindings).compute(node); }
        catch (e) { return undefined; }
    };

    // Text Selector Generator
    const textQuerySelectorAll = function*(root, text) {
        const xpath = `//text()[contains(., ${JSON.stringify(text)})]/parent::*`;
        try {
            const snapshot = document.evaluate(xpath, root, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
            for (let i = 0; i < snapshot.snapshotLength; i++) yield snapshot.snapshotItem(i);
        } catch (e) {}
    };

    const computeTextSelector = (node) => {
        const content = (node.innerText || node.textContent || '').trim();
        if (!content) return;
        if (content.length <= 12) {
            const elements = [];
            for (const value of textQuerySelectorAll(document, content)) { elements.push(value); if (elements.length >= 2) break; }
            return elements.length === 1 && elements[0] === node ? [content] : undefined;
        }
        if (content.length > 64) return;
        let left = 12, right = content.length;
        while (left <= right) {
            const center = left + ((right - left) >> 2);
            const elements = [];
            for (const value of textQuerySelectorAll(document, content.slice(0, center))) { elements.push(value); if (elements.length >= 2) break; }
            if (elements.length !== 1 || elements[0] !== node) { left = center + 1; } else { right = center - 1; }
        }
        if (right === content.length) return;
        const length = right + 1;
        const remainder = content.slice(length, length + 64);
        const match = remainder.search(/ |$/);
        return [content.slice(0, length + (match > -1 ? match : 0))];
    };

    // =====================================================================
    // 2. AUTOMATIQ RECORDING LOGIC
    // =====================================================================

    // 1. Track Every Keypress (Restored)
    document.addEventListener('keydown', (e) => {
        if (['Shift', 'Control', 'Alt', 'Meta'].includes(e.key)) return;
        const target = (e.composedPath && e.composedPath()[0]) || e.target;
        emitAction({
            type: 'keypress',
            key: e.key,
            code: e.code,
            tag: target.tagName,
            value: target.value || ''
        });
    }, true);

    // 2. Track Input Changes (Restored)
    document.addEventListener('change', (e) => {
        if (['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) {
            emitAction({
                type: 'input',
                tag: e.target.tagName,
                name: e.target.name || e.target.id || '',
                value: e.target.value
            });
        }
    }, true);

    // 3. Track Clicks (Upgraded with Locators & Delay)
    document.addEventListener('mousedown', (e) => {
        // composedPath()[0] pierces shadow DOM to get the real clicked element
        const rawTarget = (e.composedPath && e.composedPath()[0]) || e.target;
        if (!(rawTarget instanceof Element)) return;

        // Try to find the best interactive ancestor for locator quality,
        // but ALWAYS fall back to rawTarget — never silently drop the click
        const interactiveSelector = `button, a, input, select, textarea, label, [role], [tabindex]:not([tabindex="-1"]), [onclick]`;
        const target = rawTarget.closest(interactiveSelector) || rawTarget;

        // Compute strict DOM coordinates immediately
        const cssPath = computeCSSSelector(target);
        const ariaPath = computeARIASelector(target);
        const textPath = computeTextSelector(target);

        // Wait 50ms for React/Vue/Google Forms to update visually before reading attributes
        setTimeout(() => {
            emitAction({
                type: 'click',
                tag: target.tagName,
                text: (target.innerText || target.value || '').substring(0, 100).trim(),
                id: target.id || '',
                href: target.href || '',
                role: target.getAttribute('role') || '',
                ariaLabel: target.getAttribute('aria-label') || '',
                ariaChecked: target.getAttribute('aria-checked') || '',
                dataValue: target.dataset?.value || '',
                inputType: target.type || '',
                locators: {
                    css: cssPath ? cssPath.join(' > ') : null,
                    aria: ariaPath ? ariaPath.join(' > ') : null,
                    text: textPath ? textPath[0] : null
                }
            });
        }, 50);
    }, true);

    // 4. Track Page Changes (SPA & Hash changes) (Restored)
    let lastUrl = location.href;
    new MutationObserver(() => {
        if (location.href !== lastUrl) {
            lastUrl = location.href;
            emitAction({ type: 'page_changed', newUrl: location.href, reason: 'mutation' });
        }
    }).observe(document, { subtree: true, childList: true });

    window.addEventListener('popstate', () => {
        lastUrl = location.href;
        emitAction({ type: 'page_changed', newUrl: location.href, reason: 'popstate' });
    });

    window.addEventListener('hashchange', () => {
        lastUrl = location.href;
        emitAction({ type: 'page_changed', newUrl: location.href, reason: 'hashchange' });
    });

    // Popups opened via window.open are detected on the Python side through
    // CDP TargetCreated events, so no JS hook is needed here (and an isolated
    // content script cannot override the page's main-world window.open anyway).

})();
