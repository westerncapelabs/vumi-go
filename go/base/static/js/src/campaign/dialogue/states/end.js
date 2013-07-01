// go.campaign.dialogue.states.end
// ===============================
// Structures for end states (states which display something to the user and
// end the session)

(function(exports) {
  var states = go.campaign.dialogue.states,
      DialogueStateView = states.DialogueStateView,
      DialogueStateEditView = states.DialogueStateEditView,
      DialogueStatePreviewView = states.DialogueStatePreviewView;

  var EndStateEditView = DialogueStateEditView.extend({
    bodyTemplate: JST.campaign_dialogue_states_end_edit,

    save: function() {
      this.state.model.set('text', this.$('.text').val(), {silent: true});
      return this;
    }
  });

  var EndStatePreviewView = DialogueStatePreviewView.extend({
    bodyTemplate: JST.campaign_dialogue_states_end_preview
  });

  var EndStateView = DialogueStateView.extend({
    typeName: 'end',

    editModeType: EndStateEditView,
    previewModeType: EndStatePreviewView,

    endpointSchema: [{attr: 'entry_endpoint', side: 'left'}]
  });

  _(exports).extend({
    EndStateView: EndStateView,

    EndStateEditView: EndStateEditView,
    EndStatePreviewView: EndStatePreviewView
  });
})(go.campaign.dialogue.states.end = {});
