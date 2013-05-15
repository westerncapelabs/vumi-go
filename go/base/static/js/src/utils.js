// go.utils
// ========
// Utilities and helpers for Go

(function(exports) {
  // Acts as a 'base' for class-like objects which can be extended (with the
  // prototype chain set up automatically)
  exports.Extendable = function () {};

  // Backbone has an internal `extend()` function which it assigns to its
  // structures. We need this function, so we arbitrarily choose
  // `Backbone.Model`, since it has the `extend()` function we are looking for.
  var extend = Backbone.Model.extend;

  exports.Extendable.extend = function() {
    Array.prototype.unshift.call(arguments, {parent: this.prototype});
    return extend.call(this, _.extend.apply(this, arguments));
  };

  // Class-like object on which events can be bound and emitted
  exports.Eventable = exports.Extendable.extend(Backbone.Events, {
    events: {},
    constructor: function() { exports.delegateEvents(this, this.events); }
  });

  // Binds events to callbacks on an Eventable or Backbone Model/View instance
  exports.delegateEvents = function(that, events) {
      for (var e in events) { that.on(e, that[events[e]].bind(that)); }
  };

  // Pop a property off an object
  exports.pop = function(collection, key) {
    var value = collection[key];
    delete collection[key];
    return value;
  };
})(go.utils = {});
