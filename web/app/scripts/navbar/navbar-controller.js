(function () {
    'use strict';

    /* ngInject */
    function NavbarController($rootScope, $scope, $state, $modal,
                              AuthService, BoundaryState, GeographyState, InitialState,
                              MapState, RecordState, UserService, WebConfig) {
        var ctl = this;
        var initialized = false;

        var userDropdownDefault = 'My Account';

        InitialState.ready().then(init);

        ctl.onLogoutButtonClicked = AuthService.logout;
        ctl.authenticated = AuthService.isAuthenticated();
        ctl.hasWriteAccess = AuthService.hasWriteAccess();
        ctl.isAdmin = AuthService.isAdmin();
        ctl.onGeographySelected = onGeographySelected;
        ctl.onBoundarySelected = onBoundarySelected;
        ctl.onRecordTypeSelected = onRecordTypeSelected;
        ctl.onStateSelected = onStateSelected;
        ctl.navigateToStateName = navigateToStateName;
        ctl.showAuditDownloadModal = showAuditDownloadModal;
        ctl.getBoundaryLabel = getBoundaryLabel;
        ctl.recordTypesVisible = WebConfig.recordType.visible;
        ctl.userEmail = userDropdownDefault;

        $rootScope.$on('$stateChangeSuccess', setStates);

        function init() {
            setFilters($state.current);
            GeographyState.getOptions().then(function(opts) { ctl.geographyResults = opts; });
            RecordState.getOptions().then(function(opts) { ctl.recordTypeResults = opts; });
            setStates();
            UserService.getUser(AuthService.getUserId()).then(function(userInfo) {
                if (userInfo && userInfo.email) {
                    ctl.userEmail = userInfo.email;
                } else {
                    ctl.userEmail = userDropdownDefault;
                }
            });
            initialized = true;
        }

        $rootScope.$on('$stateChangeSuccess', function(event, toState) {
            ctl.authenticated = AuthService.isAuthenticated();
            setFilters(toState);
        });

        // Record Type selections
        $scope.$on('driver.state.recordstate:options', function(event, options) {
            ctl.recordTypeResults = options;
        });
        $scope.$on('driver.state.recordstate:selected', function(event, selected) {
            ctl.recordTypeSelected = selected;
            updateState();
        });

        // Boundary selections
        $scope.$on('driver.state.boundarystate:options', function(event, options) {
            ctl.boundaryResults = options;
        });
        $scope.$on('driver.state.boundarystate:selected', function(event, selected) {
            ctl.boundarySelected = selected;
            updateState();
            MapState.setLocation(null);
            MapState.setZoom(null);
        });

        // Geography selections
        $scope.$on('driver.state.geographystate:options', function(event, options) {
            ctl.geographyResults = options;
        });
        $scope.$on('driver.state.geographystate:selected', function(event, selected) {
            ctl.geographySelected = selected;
            // Need to get the new list of boundaries for the selected geography.
            // Only do this after initializing: otherwise an unneeded request is sent.
            if (initialized) {
                BoundaryState.updateOptions({boundary: selected.uuid}).then(function() {
                    updateState();
                });
            }
        });

        // A function to set properties related to whether or not the filterbar should be instantiated for a given page
        function setFilters(state) {
            var filterPages = ['Map', 'Record List'];
            var isFilterPage = _.includes(filterPages, state.label);
            ctl.isFilterPage = isFilterPage;
        }

        // Sets states that can be navigated to (exclude current state, since we're already there)
        function setStates() {
            ctl.stateSelected = $state.current;
            ctl.availableStates = _($state.get())
                .map(function(name) { return $state.get(name); })
                .filter(function(state) {
                    return state.showInNavbar && state.name !== $state.get().name;
                })
                .value();
        }

        // Updates the ui router state based on selected navigation parameters
        function updateState() {
            if (ctl.stateSelected) {
                $state.go(ctl.stateSelected.name);
            }
        }

        // Handler for when a geography is selected from the dropdown
        function onGeographySelected(geography) {
            GeographyState.setSelected(geography);
        }

        // Handler for when a boundary is selected from the dropdown
        function onBoundarySelected(boundary) {
            BoundaryState.setSelected(boundary);
        }

        // Handler for when a record type is selected from the dropdown
        function onRecordTypeSelected(recordType) {
            RecordState.setSelected(recordType);
        }

        // Handler for when a navigation state is selected from the dropdown
        function onStateSelected(navState) {
            ctl.stateSelected = navState;
            updateState();
        }

        // Handler for when a navigation state is selected from the dropdown
        function navigateToStateName(stateName) {
            onStateSelected($state.get(stateName));
        }

        // Returns the label for a boundary, based on the currently selected geography
        // TODO: this should eventually be moved to an angular filter if needed elsewhere
        function getBoundaryLabel(boundary) {
            if (!boundary || !boundary.data || !ctl.geographySelected) {
                return 'All';
            }
            /* jshint camelcase: false */
            return boundary.data[ctl.geographySelected.display_field];
            /* jshint camelcase: true */
        }

        // Show a details modal for the given record
        function showAuditDownloadModal() {
            $modal.open({
                templateUrl: 'scripts/audit/audit-download-modal-partial.html',
                controller: 'AuditDownloadModalController as modal',
                size: 'sm',
                backdrop: 'static',
            });
        }
    }

    angular.module('driver.navbar')
    .controller('NavbarController', NavbarController);

})();
