import copy
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Set, Tuple
import srsly
from spacy.util import ensure_path

from .hashing import dataset_hash
from .loaders import read_json, read_jsonl
from .operations import registry
from .registry import loading_pipelines
from .store import ExampleStore
from .types import DatasetOperationsState, Example, OperationResult, OperationState, OperationStatus


class Dataset:
    """A Dataset is a around a List of Examples.
    Datasets are responsible for tracking all Operations done on them.
    This ensures data lineage and easy reporting of how changes in the 
    data based on various Operations effects overall quality.
    
    Dataset holds state (let's call it self.operations for now)
    self.operations is a list of every function run on the Dataset since it's
    initial creation. If loading from disk, track everything that happens in loading
    phase in operations as well by simply initializing self.operations in constructors
    
    Each operation should has the following attributes:
        operation hash
        name: function/callable name ideally, could be added with a decorator
        status: (not_started|completed)
        transformations: List[Transformation]
            commit hash
            timestamp(s) - start and end both? end is probably enough
            examples deleted
            examples added
            examples corrected
            annotations deleted
            annotations added
            annotations corrected

            for annotations deleted/added/corrected, include mapping from old Example hash to new Example hash
            that can be decoded for display later

    All operations are serializable in the to_disk and from_disk methods.

    So if I have 10 possible transformations.

    I can run 1..5, save to disk train a model and check results. 
    Then I can load that model from disk with all previous operations already tracked
    in self.operations. Then I can run 6..10, save to disk and train model.
    Now I have git-like "commits" for the data used in each model.
    
    """

    def __init__(
        self,
        name: str,
        data: List[Example] = [],
        operations: List[OperationState] = None,
        example_store: ExampleStore = None
    ):
        self.name = name
        self.data = data
        if not operations:
            operations = []
        self.operations = operations

        if example_store is None:
            example_store = ExampleStore(data)
        self.example_store = example_store
    
    @property
    def commit_hash(self):
        return dataset_hash(self, as_int=False)

    def __hash__(self):
        return dataset_hash(self)
    
    def __len__(self):
        return len(self.data)
    
    def apply(
        self, func: Callable[[List[Example], Any, Any], Any], *args: Any, **kwargs: Any
    ) -> Any:
        """Apply a function to the dataset
        
        Args:
            func (Callable[[List[Example], Any, Any], Any]): 
                Function from an existing recon module that can operate on a List of Examples
        
        Returns:
            Result of running func on List of Examples
        """
        return func(self.data, *args, **kwargs) # type: ignore
        
    def apply_(
        self, operation: Callable[[Any], OperationResult], *args: Any, initial_state: OperationState = None, **kwargs: Any
    ) -> None:
        """Apply a function to all data inplace.
        
        Args:
            func (Callable[[List[Example], Any, Any], List[Example]]): Function from an existing recon module
                that can operate on a List[Example] and return a List[Example]
        """
        result: OperationResult = operation(self, *args, initial_state=initial_state, **kwargs)  # type: ignore
        self.operations.append(result.state)
        dataset_changed = any((result.state.examples_added, result.state.examples_removed, result.state.examples_changed))
        if dataset_changed:
            self.data = result.data

    def from_disk(
        self,
        path: Path,
        loader_func: Callable = read_jsonl,
        loading_pipeline: List[str] = loading_pipelines.get("default")()
    ) -> "Dataset":
        """Load Dataset from disk given a path and a loader function that reads the data
        and returns an iterator of Examples
        
        Args:
            path (Path): path to load from
            loader_func (Callable, optional): Callable that reads a file and returns a List of Examples. 
                Defaults to [read_jsonl][recon.loaders.read_jsonl]
        """
        path = ensure_path(path)
        ds_op_state = None
        if (path.parent / ".recon" / self.name).exists():
            state = srsly.read_json(path.parent / ".recon" / self.name / "state.json")
            ds_op_state = DatasetOperationsState(**state)
            self.operations = ds_op_state.operations

        data = loader_func(path, loading_pipeline=loading_pipeline)
        self.data = data

        if ds_op_state and self.commit_hash != ds_op_state.commit:
            # Dataset changed, examples added
            self.operations.append(
                OperationState(
                    name="examples_added_external",
                    status=OperationStatus.COMPLETED,
                    ts=datetime.now(),
                    examples_added=max(len(self) - ds_op_state.size, 0),
                    examples_removed=max(ds_op_state.size - len(self), 0),
                    examples_changed=0,
                    transformations=[]
                )
            )

            for op in self.operations:
                op.status = OperationStatus.NOT_STARTED

        seen: Set[str] = set()
        operations_to_run: Dict[str, OperationState] = {}

        for op in self.operations:
            if op.name not in operations_to_run and op.name in registry.operations:
                operations_to_run[op.name] = op

        print(operations_to_run.keys())

        for op_name, state in operations_to_run.items():
            op = registry.operations.get(op_name)
            self.apply_(op, *state.args, initial_state=state, **state.kwargs)

        return self


    def to_disk(self, output_path: Path, force: bool = False, save_examples: bool = True) -> None:
        """Save Corpus to Disk
        
        Args:
            output_path (Path): Output file path to save data to
            force (bool): Force save to directory. Create parent directories
                or overwrite existing data.
            save_examples (bool): Save the example store along with the state.
        """
        output_path = ensure_path(output_path)
        output_dir = output_path.parent
        state_dir = output_dir / ".recon" / self.name
        if force:
            output_dir.mkdir(parents=True, exist_ok=True)
        
            if not state_dir.exists():
                state_dir.mkdir(parents=True, exist_ok=True)

        ds_op_state = DatasetOperationsState(name=self.name, commit=self.commit_hash, size=len(self), operations=self.operations)
        srsly.write_json(state_dir / "state.json", ds_op_state.dict())
        # with (state_dir / "state.json").open("w+") as state_f:
        #     state_f.write(ds_op_state.json(indent=4))

        if save_examples:
            self.example_store.to_disk(state_dir / "example_store.jsonl")

        srsly.write_jsonl(output_path, [e.dict() for e in self.data])