---
rule_kind: formElementRule
intent: show this field only until specific values have been recorded in a prior encounter
  (e.g. show TD Booster until both TD 1 and TD 2 have already been given)
entity_param: programEncounter
encounter_types: []
concepts: []
source_org: JNPCT
field_name: TD Booster
kind: visibility
---
```js
'use strict';
({params, imports}) => {
  const programEncounter = params.entity;
  const formElement = params.formElement;
     
  if (new imports.rulesConfig.RuleCondition({programEncounter, formElement}).when.latestValueInPreviousEncounters("TD 1")
            .is.defined.and.when.latestValueInPreviousEncounters("TD 2")
            .is.defined.matches()) {
            return new imports.rulesConfig.FormElementStatus(formElement.uuid, false);
        }else if (new imports.rulesConfig.RuleCondition({programEncounter, formElement}).when.latestValueInPreviousEncounters("TD Booster").is.defined.matches()){
            return new imports.rulesConfig.FormElementStatus(formElement.uuid, false);
        } else {
        return new imports.rulesConfig.FormElementStatus(formElement.uuid, true);
        }

};
```
