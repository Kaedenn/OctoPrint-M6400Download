const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const requests = [];
const dialogs = [];
const notifications = [];
const button = {classes: new Set(), file: null};

function $(value) {
    if (typeof value === "function") {
        value();
        return;
    }
    return {
        hasClass: (name) => value.classes.has(name),
        addClass(name) { value.classes.add(name); return this; },
        removeClass(name) { value.classes.delete(name); return this; }
    };
}

const ko = {
    bindingHandlers: {},
    dataFor: (element) => element.file,
    pureComputed: (read) => {
        const computed = () => read();
        computed.extend = () => computed;
        return computed;
    }
};

const context = {
    $, ko, window: {}, OCTOPRINT_VIEWMODELS: [], console,
    _: {
        escape: (value) => String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;"),
        sprintf: (format, values) => format.replace(/%\((\w+)\)s/g, (_match, key) => values[key])
    },
    PNotify: function(options) { notifications.push(options); },
    gettext: (message) => message,
    showConfirmationDialog: (options) => dialogs.push(options),
    OctoPrint: {
        simpleApiCommand: (_plugin, _command, payload) => {
            const request = {payload};
            requests.push(request);
            return {fail: (callback) => { request.fail = callback; }};
        }
    }
};

const source = fs.readFileSync(
    path.join(__dirname, "../octoprint_M6400Download/static/js/M6400Download.js"),
    "utf8"
);
vm.runInNewContext(source, context);
const ViewModel = context.OCTOPRINT_VIEWMODELS[0].construct;
const access = {permissions: {FILES_DOWNLOAD: "download"}};
const model = new ViewModel([{}, {hasPermission: () => true}, access, {}]);

function click(file) {
    button.file = file;
    model._downloadClickHandler(button, {preventDefault() {}});
    return requests[requests.length - 1];
}

let request = click({origin: "printer", type: "machinecode", path: "LONGNA~1.GCO", display: "LongName.gcode"});
assert.strictEqual(request.payload.filename, "LongName.gcode");
assert.strictEqual(request.payload.force, false);

request.fail({status: 409, responseJSON: {error: "file_exists"}});
assert.strictEqual(dialogs.length, 1);
dialogs[0].onproceed();
assert.strictEqual(requests[1].payload.filename, "LongName.gcode");
assert.strictEqual(requests[1].payload.force, true);
dialogs[0].onclose();
assert(button.classes.has("disabled"));
requests[1].fail({status: 500});

request = click({origin: "printer", type: "machinecode", path: "folder/LONGNA~1.GCO", display: "LongName.gcode"});
assert.strictEqual(request.payload.filename, "folder/LongName.gcode");
request.fail({status: 500});

request = click({origin: "printer", type: "machinecode", path: "LONGNA~1.GCO", display: "Long Name.gcode"});
assert.strictEqual(request.payload.filename, "LONGNA~1.GCO");
request.fail({status: 500});

request = click({origin: "printer", type: "machinecode", path: "SHORT.GCO"});
assert.strictEqual(request.payload.filename, "SHORT.GCO");
request.fail({status: 500});

model.onDataUpdaterPluginMessage("M6400Download", {
    type: "download_complete",
    path: "folder/LONGNA~1.GCO",
    file: "folder/LongName.gcode"
});
assert.strictEqual(notifications.length, 1);
assert.strictEqual(notifications[0].type, "success");
assert.strictEqual(
    notifications[0].text,
    "Completed downloading folder/LONGNA~1.GCO to folder/LongName.gcode"
);
model.onDataUpdaterPluginMessage("other_plugin", {type: "download_complete"});
model.onDataUpdaterPluginMessage("M6400Download", {type: "download_failed"});
assert.strictEqual(notifications.length, 1);

console.log("M6400 view model filename checks passed");
