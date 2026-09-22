/*
 * View model for OctoPrint-M6400Download
 *
 * Author: Kaedenn A. D. N.
 * License: AGPL-3.0-or-later
 */
$(function() {
    ko.bindingHandlers.m6400Debugging = {
        init: function(element, valueAccessor) {
            var settingsViewModel = ko.unwrap(valueAccessor());

            function bindSetting() {
                var settings = settingsViewModel.settings;
                var pluginSettings = settings && settings.plugins && settings.plugins.M6400Download;
                if (pluginSettings && pluginSettings.debugging) {
                    ko.applyBindingsToNode(element, {
                        checked: pluginSettings.debugging
                    });
                }
            }

            // Settings templates may be bound before the initial settings API
            // request completes. Bind the checkbox only once its observable is
            // available instead of dereferencing an undefined settings object.
            if (settingsViewModel.settings) {
                bindSetting();
            } else if (settingsViewModel.firstRequest) {
                settingsViewModel.firstRequest.done(bindSetting);
            }
        }
    };

    class M6400DownloadViewModel {
        constructor(parameters) {
            window.M6400DownloadViewModel = this;
            this.filesViewModel = parameters[0];
            this.loginState = parameters[1];
            this.access = parameters[2];
            this.settingsViewModel = parameters[3];
            // OctoPrint initializes access.permissions after constructing view models.
            this.downloadPermission = ko.pureComputed(() =>
                this.loginState.hasPermission(this.access.permissions.FILES_DOWNLOAD)
            ).extend({notify: "always"});
            this.downloadObserver = null;
            this._boundDownloadClickHandler = (event) => {
                this._downloadClickHandler(event.currentTarget, event);
            };
        }

        _isDebuggingEnabled() {
            var settings = this.settingsViewModel.settings;
            var pluginSettings = settings && settings.plugins && settings.plugins.M6400Download;
            return !!(pluginSettings && pluginSettings.debugging && pluginSettings.debugging());
        }

        _debug(message, details) {
            if (!this._isDebuggingEnabled()) {
                return;
            }
            console.debug("[M6400Download] " + message, details || {});
        }

        _isPrinterSdFile(file) {
            var result = file &&
                (file.origin === "printer" || file.origin === "sdcard") &&
                file.type !== "folder";
            return result;
        }

        _enablePrinterDownloadButton(button) {
            var file = ko.dataFor(button);
            if (!this._isPrinterSdFile(file)) {
                return;
            }

            // OctoPrint disables this anchor when the printer does not supply
            // a native download URL. M6400 provides that missing transport.
            $(button).removeClass("disabled").removeAttr("disabled").attr("href", "#");
        }

        _enablePrinterDownloadButtons() {
            if (!this.downloadPermission()) {
                this._debug("Skipped enabling SD buttons; File Download permission is absent");
                return;
            }
            $("a.btn-files-download").each((index, button) => {
                this._enablePrinterDownloadButton(button);
            });
        }

        _downloadClickHandler(button, event) {
            var file = ko.dataFor(button);
            if (!this._isPrinterSdFile(file) || $(button).hasClass("disabled")) {
                return;
            }

            event.preventDefault();
            $(button).addClass("disabled");
            this._requestDownload(button, file, false);
        }

        _downloadFilename(file) {
            if (!file.display || /\s/.test(file.display)) {
                return file.path;
            }
            var slash = file.path.lastIndexOf("/");
            return file.path.slice(0, slash + 1) + file.display;
        }

        _requestDownload(button, file, force) {
            var filename = this._downloadFilename(file);
            this._debug("Requesting SD download", {filename: filename, file: file, force: force});
            OctoPrint.simpleApiCommand("M6400Download", "download", {
                filename: filename,
                force: force
            }).fail((response) => {
                if (!force && response.status === 409 &&
                    response.responseJSON && response.responseJSON.error === "file_exists") {
                    let confirmed = false;
                    showConfirmationDialog({
                        title: gettext("Overwrite file?"),
                        message: gettext("A local file with this name already exists. It will be overwritten."),
                        question: gettext("Do you want to continue?"),
                        cancel: gettext("No"),
                        proceed: gettext("Yes"),
                        onproceed: () => {
                            confirmed = true;
                            this._requestDownload(button, file, true);
                        },
                        onclose: () => {
                            if (!confirmed) {
                                $(button).removeClass("disabled");
                            }
                        }
                    });
                    return;
                }
                $(button).removeClass("disabled");
                this._debug("SD download request failed; button re-enabled", {filename: filename});
            });
        }

        _installDownloadIntegration() {
            if (!this.downloadPermission() || this.downloadObserver) {
                this._debug("Skipped download integration installation", {
                    hasPermission: this.downloadPermission(),
                    alreadyInstalled: !!this.downloadObserver
                });
                return;
            }

            this._enablePrinterDownloadButtons();
            $(document).on("click.m6400Download", "a.btn-files-download", this._boundDownloadClickHandler);
            this.downloadObserver = new MutationObserver((mutations) => {
                this._enablePrinterDownloadButtons();
            });
            this.downloadObserver.observe(document.body, {
                childList: true,
                subtree: true
            });
            this._debug("Installed SD download integration");
        }

        _removeDownloadIntegration() {
            if (!this.downloadObserver) {
                this._debug("Skipped download integration removal; not installed");
                return;
            }

            this.downloadObserver.disconnect();
            this.downloadObserver = null;
            $(document).off("click.m6400Download", "a.btn-files-download", this._boundDownloadClickHandler);
            $("a.btn-files-download").each((index, button) => {
                if (this._isPrinterSdFile(ko.dataFor(button))) {
                    $(button).addClass("disabled").removeAttr("href");
                }
            });
            this._debug("Removed SD download integration and disabled SD buttons");
        }

        onDataUpdaterPluginMessage(plugin, data) {
            if (plugin !== "M6400Download" || !data ||
                data.type !== "download_complete" || !this.downloadPermission()) {
                return;
            }
            new PNotify({
                title: gettext("Download complete"),
                text: _.sprintf(gettext("Completed downloading %(path)s to %(file)s"), {
                    path: _.escape(data.path),
                    file: _.escape(data.file)
                }),
                type: "success"
            });
        }

        onStartupComplete() {
            this.downloadPermission.subscribe((allowed) => {
                this._debug("File Download permission changed", {allowed: allowed});
                if (allowed) {
                    this._installDownloadIntegration();
                } else {
                    this._removeDownloadIntegration();
                }
            });
            this._debug("M6400Download view model startup complete", {
                hasDownloadPermission: this.downloadPermission()
            });
            this._installDownloadIntegration();
        }
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: M6400DownloadViewModel,
        dependencies: [
            "filesViewModel",
            "loginStateViewModel",
            "accessViewModel",
            "settingsViewModel"
        ],
        elements: [ /* ... */ ]
    });
});

/* vim: set ts=4 sts=4 sw=4: */
